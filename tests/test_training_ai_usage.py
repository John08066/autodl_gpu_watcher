import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from autodl_watcher.training_ai import analyze, settings, AnalysisError, DEFAULT_PROMPT, save_settings, load_settings
from autodl_watcher.training_ai_usage import LUNA_PRICES, UsageStore, estimate, tokens, call_text
from autodl_watcher.training_ai_ui import AIAssistant, profile_text
from autodl_watcher.training_ui import TrainingService
from test_training_ai import SOURCE, RESULT, CONFIG, sse

PRICING = {**LUNA_PRICES, 'model':CONFIG['model'], 'base_url':CONFIG['base_url']}
CONFIG_PRICE = {**CONFIG, 'pricing':PRICING}
USAGE = {'input_tokens':1000,'output_tokens':300,'input_tokens_details':{'cached_tokens':200,'cache_write_tokens':100}}


def event(result=RESULT, usage=USAGE, tier='flex', kind='response.completed'):
    return sse({'type':kind,'response':{'status':'completed' if kind == 'response.completed' else 'incomplete',
               'model':CONFIG['model'],'service_tier':tier,'usage':usage,'output':[{'content':[{'type':'output_text','text':json.dumps(result)}]}]}})


class UsageTest(unittest.TestCase):
    def call(self, lines, config=CONFIG_PRICE):
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value='test-only-key'), patch('autodl_watcher.training_ai.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            response=session.post.return_value.__enter__.return_value
            response.status_code=200;response.iter_lines.return_value=lines
            result=analyze(config,SOURCE)
            self.assertEqual(session.post.call_count,1)
            return result, session.post.call_args.kwargs['json']

    def test_responses_actual_usage_cache_write_replaces_input_and_tier(self):
        result,_=self.call([event()])
        call=result['call']
        self.assertEqual(call['tokens'],{'input':1000,'output':300,'cached':200,'write':100})
        self.assertAlmostEqual(call['usd'],.00011725)
        self.assertAlmostEqual(call['cny'],.00082075)
        standard,_=self.call([event(tier='default')])
        self.assertAlmostEqual(standard['call']['usd'],call['usd']*2)
        self.assertEqual(standard['requested_tier'],'flex')

    def test_chat_empty_choice_usage_chunk_and_request_option(self):
        usage={'prompt_tokens':1000,'completion_tokens':300,'prompt_tokens_details':{'cached_tokens':200,'cache_write_tokens':100}}
        lines=[sse({'choices':[{'delta':{'content':json.dumps(RESULT)},'finish_reason':'stop'}]}),
               sse({'choices':[],'model':CONFIG['model'],'service_tier':'flex','usage':usage}),b'data: [DONE]']
        result,payload=self.call(lines,{**CONFIG_PRICE,'protocol':'chat'})
        self.assertTrue(payload['stream_options']['include_usage'])
        self.assertAlmostEqual(result['call']['usd'],.00011725)

    def test_missing_usage_and_partial_cache_are_not_reported_as_free(self):
        result,_=self.call([event(usage=None)])
        self.assertIsNone(result['call']['usd'])
        self.assertIn('未知',call_text(result['call']))
        result,_=self.call([event(usage={'input_tokens':1000,'output_tokens':300})])
        self.assertIn('缓存明细缺项',result['call']['cost_note'])
        self.assertIsNone(result['call']['tokens']['cached'])

    def test_invalid_rule_and_incomplete_response_keep_returned_usage(self):
        for line,status in ((event(result={'name':'bad','rules':[]}),'规则验证失败'),(event(kind='response.incomplete'),'请求失败')):
            with self.subTest(status=status),self.assertRaises(AnalysisError) as context:
                self.call([line])
            self.assertEqual(context.exception.call['status'],status)
            self.assertEqual(context.exception.call['tokens']['input'],1000)
            self.assertAlmostEqual(context.exception.call['usd'],.00011725)

    def test_invalid_rule_is_written_once_without_log_or_key(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ai.json';path.write_text(json.dumps(CONFIG_PRICE))
            ai=AIAssistant(None,path,TrainingService(queue.Queue()))
            with patch('autodl_watcher.training_ai.ai_credentials.read',return_value='test-only-key'), patch('autodl_watcher.training_ai.requests.Session') as factory:
                response=factory.return_value.__enter__.return_value.post.return_value.__enter__.return_value
                response.status_code=200;response.iter_lines.return_value=[event(result={'name':'bad','rules':[]})]
                ai._run('identity','server',10,settings(CONFIG_PRICE),SOURCE)
            rows=ai.usage.records()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['status'],'规则验证失败')
            serialized=json.dumps(rows)
            self.assertNotIn(SOURCE,serialized);self.assertNotIn('test-only-key',serialized)
            self.assertIn('累计：1次请求',ai.usage.summary())

    def test_pricing_snapshot_unknown_model_and_official_defaults(self):
        config=settings(CONFIG_PRICE)
        call={'tokens':tokens(USAGE),'actual_tier':'flex','actual_model':CONFIG['model']}
        result=estimate(call,config)
        config['pricing']['input']=123
        self.assertEqual(result['pricing']['input'],.10)
        self.assertIsNone(estimate(call,settings({**CONFIG_PRICE,'model':'other'}))['usd'])
        official=settings({'model':'gpt-6-luna'})
        self.assertEqual(official['pricing']['input'],.10)
        self.assertIsNone(settings(CONFIG)['pricing']['input'])

    def test_prompt_persists_changes_payload_and_keeps_fixed_contract(self):
        prompt='重点监控Dice、FID、学习率和验证阶段。'
        result,payload=self.call([event()],{**CONFIG_PRICE,'prompt':prompt})
        content=payload['input'][0]['content']
        self.assertIn(prompt,content)
        self.assertIn('不可覆盖的输出契约',content)
        self.assertIn('fields',content)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ai.json'
            save_settings(path,{**CONFIG_PRICE,'prompt':prompt})
            self.assertEqual(load_settings(path)['prompt'],prompt)
        self.assertEqual(settings(CONFIG)['prompt'],DEFAULT_PROMPT)
        with self.assertRaises(AnalysisError):settings({**CONFIG,'prompt':''})

    def test_profile_text_does_not_claim_application_without_current_card(self):
        text=profile_text({'profile':RESULT},'主卡片：默认监控 · 当前片段未匹配规则')
        self.assertIn('当前片段未匹配规则',text)
        self.assertNotIn('已用于主卡片',text)


if __name__ == '__main__': unittest.main()
