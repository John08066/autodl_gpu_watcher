import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import MagicMock, patch

import requests
from autodl_watcher import ai_credentials
from autodl_watcher.training_ai import AnalysisError, analyze, decode_result, excerpt, save_settings, settings
from autodl_watcher.training_ai_ui import AIAssistant, AISettings
from autodl_watcher.training_ui import TrainingService
from autodl_watcher.training import TrainingServer
from test_training import snapshot
import queue

CONFIG = {"base_url": "https://example.com/v1", "protocol": "responses", "model": "example-model", "service_tier": "flex", "auto_generate": False}
SOURCE = "Epoch 2 Dice=0.81 custom_quality=4.2"
RESULT = {"name": "Dice规则", "rules": [{"format": "text", "template": "Epoch {epoch:number} Dice={dice:number} custom_quality={quality:number}", "phase": "train", "fields": {"epoch": "epoch"}, "metrics": {"Dice": "dice", "质量": "quality"}, "example": SOURCE}]}


def sse(event):
    return b"data: " + json.dumps(event).encode()


def completed():
    return sse({"type": "response.completed", "response": {"status": "completed", "service_tier": "flex", "output": [{"content": [{"type": "output_text", "text": json.dumps(RESULT)}]}]}})


class AIBackendTest(unittest.TestCase):
    def call(self, lines, config=None, code=200, failure=None):
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value='test-secret-value'), patch('autodl_watcher.training_ai.requests.Session') as factory:
            session=factory.return_value.__enter__.return_value
            response=session.post.return_value.__enter__.return_value
            response.status_code=code
            response.iter_lines.return_value=lines
            if failure:
                session.post.side_effect=failure
            try:
                result=analyze(config or CONFIG,SOURCE)
            finally:
                self.assertEqual(session.post.call_count,1)
            return result,session.post.call_args

    def test_responses_complete_and_no_redirect_or_store(self):
        result,call=self.call([completed()])
        self.assertEqual(result['profile'],RESULT)
        self.assertEqual(result['service_tier'],'flex')
        self.assertEqual(call.kwargs['json']['service_tier'],'flex')
        self.assertEqual(call.kwargs['timeout'],(10,900))
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertFalse(call.kwargs['json']['store'])
        self.assertTrue(call.kwargs['stream'])
        self.assertNotIn('test-secret-value',json.dumps(call.kwargs['json']))

    def test_chat_stop_and_done_required(self):
        lines=[sse({'choices':[{'index':0,'delta':{'content':json.dumps(RESULT)},'finish_reason':None}]}),sse({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}),b'data: [DONE]']
        result,call=self.call(lines,{**CONFIG,'protocol':'chat'})
        self.assertEqual(result['profile'],RESULT)
        self.assertTrue(call.args[0].endswith('/chat/completions'))
        with self.assertRaisesRegex(AnalysisError,'完成标记'):
            self.call(lines[:-1],{**CONFIG,'protocol':'chat'})

    def test_eof_failed_truncated_and_http_error_do_not_retry(self):
        cases=[([],200),([sse({'type':'response.incomplete'})],200),([],429),([],302),([],401)]
        for lines,code in cases:
            with self.subTest(code=code,lines=lines), self.assertRaises(AnalysisError):
                self.call(lines,code=code)
        with self.assertRaisesRegex(AnalysisError,'截断'):
            self.call([sse({'choices':[{'delta':{},'finish_reason':'length'}]})],{**CONFIG,'protocol':'chat'})

    def test_network_error_does_not_expose_secret_or_retry(self):
        with self.assertRaises(AnalysisError) as error:
            self.call([],failure=requests.ConnectionError('Bearer test-secret-value'))
        self.assertNotIn('test-secret-value',str(error.exception))

    def test_missing_key_never_sends(self):
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value=''),patch('autodl_watcher.training_ai.requests.Session') as factory,self.assertRaisesRegex(AnalysisError,'尚未保存'):
            analyze(CONFIG,SOURCE)
        factory.assert_not_called()

    def test_save_public_settings_without_key_or_msgx_namespace(self):
        with tempfile.TemporaryDirectory() as folder, patch('autodl_watcher.training_ai.ai_credentials.save') as save:
            path=Path(folder)/'ai.json'
            save_settings(path,{**CONFIG,'api_key':'not-allowed'},'test-key')
            self.assertEqual(json.loads(path.read_text()),settings(CONFIG))
            save.assert_called_once_with(CONFIG['base_url'],'test-key')
            self.assertTrue(ai_credentials._target(CONFIG['base_url']).startswith('autodl-watcher/'))
            self.assertNotEqual(ai_credentials._target(CONFIG['base_url']),ai_credentials._target('https://other.example/v1'))

    def test_invalid_profile_rejected_and_no_code_accepted(self):
        with self.assertRaises(ValueError):
            decode_result(json.dumps({'name':'x','rules':[]}),SOURCE)
        with self.assertRaises(ValueError):
            decode_result(json.dumps({**RESULT,'command':'whoami'}),SOURCE)

    def test_standard_and_unknown_returned_tier(self):
        result,call=self.call([completed()],{**CONFIG,'service_tier':'default'})
        self.assertEqual(call.kwargs['json']['service_tier'],'default')
        self.assertEqual(call.kwargs['timeout'],(10,180))
        self.assertEqual(result['service_tier'],'flex')  # 返回等级与请求分开记录。

    def test_excerpt_is_bounded_and_common_secrets_hidden(self):
        source='a'*13000+'\napi_key="secret123" password=pwd123 Authorization: Bearer bearer123 sk-somethinglong'
        result=excerpt(source)
        self.assertLessEqual(len(result),12000)
        for secret in ('secret123','pwd123','bearer123','sk-somethinglong'):
            self.assertNotIn(secret,result)
        with self.assertRaises(AnalysisError):
            settings({**CONFIG,'base_url':'https://user:pass@example.com/v1'})


class AIUITest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'ai.json'
        self.path.write_text(json.dumps(CONFIG))
        self.root=tk.Tk();self.root.withdraw()
        self.service=TrainingService(queue.Queue())
        self.assistant=AIAssistant(self.root,self.path,self.service)
        self.service.ai=self.assistant
        self.service.register(TrainingServer("s","s","host"))
        self.service.states["s"]["snapshot"]=self.service.states["s"]["tracker"].update(snapshot(SOURCE))
        self.card=self.service.states["s"]["snapshot"]["cards"][0]

    def tearDown(self):
        for timer in self.root.tk.call('after','info'):
            self.root.after_cancel(timer)
        self.root.destroy();self.temp.cleanup()

    def test_settings_save_clears_key_and_does_not_call_api(self):
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value=''),patch('autodl_watcher.training_ai.ai_credentials.save') as save,patch('autodl_watcher.training_ai.requests.Session') as network:
            dialog=AISettings(self.root,self.path)
            dialog.key.insert(0,'ui-secret')
            dialog.save()
            self.assertEqual(dialog.key.get(),'')
            self.assertNotIn('ui-secret',self.path.read_text())
            self.assertIn('未保存密钥',dialog.status.cget('text'))
            save.assert_called_once();network.assert_not_called()

    def test_global_prompt_save_restore_and_usage_page_without_api(self):
        from autodl_watcher.training_ai import DEFAULT_PROMPT
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value=''), patch('autodl_watcher.training_ai.requests.Session') as api:
            dialog=self.assistant.configure()
            dialog.prompt.delete('1.0','end');dialog.prompt.insert('1.0','监控所有分支的FID和Dice')
            dialog.save()
            self.assertEqual(self.assistant.config['prompt'],'监控所有分支的FID和Dice')
            dialog.reset_prompt();dialog.save()
            self.assertEqual(self.assistant.config['prompt'],DEFAULT_PROMPT)
            dialog.refresh_usage()
            self.assertIn('累计：0次请求',dialog.usage_text.get('1.0','end'))
            api.assert_not_called()
            dialog.destroy()

    def test_generation_window_reports_current_card_fallback_and_later_recovery(self):
        from autodl_watcher.training_rules import task_identity
        identity=task_identity(self.service.states['s']['server'],self.card)
        self.assistant.store.put(identity,{'profile':RESULT,'log_path':self.card['log_path'],'message':'专用规则已生成'})
        state=self.service.states['s']
        state['snapshot']=state['tracker'].update(snapshot('a new unrecognized format'))
        dialog=self.assistant.explain('s',self.card)
        self.assertIn('当前片段未匹配规则',dialog.output.get('1.0','end'))
        self.assertNotIn('已用于主卡片',dialog.output.get('1.0','end'))
        state['snapshot']=state['tracker'].update(snapshot(SOURCE))
        dialog.after_cancel(dialog.timer);dialog.timer=None;dialog._poll()
        self.assertIn('专用规则已启用',dialog.output.get('1.0','end'))
        dialog.destroy()

    def test_manual_only_single_request_and_main_thread_result(self):
        started,release=threading.Event(),threading.Event()
        def response(config,source,pid):
            started.set();release.wait(3)
            return {'profile':RESULT,'model':'example-model','analyzed_at':time.time(),'service_tier':'flex'}
        card=dict(self.card)
        with patch('autodl_watcher.training_ai_ui.analyze',side_effect=response) as api:
            dialog=self.assistant.explain("s",card)
            self.root.update();api.assert_not_called()
            dialog.start();self.assertTrue(started.wait(1))
            other=self.assistant.explain("s",card);other.start()
            self.assertIn('已有 AI 请求',other.status.cget('text'))
            release.set()
            deadline=time.monotonic()+3
            while str(dialog.send.cget('state'))=='disabled' and time.monotonic()<deadline:
                self.root.update();time.sleep(.02)
            self.assertIn('Dice',dialog.output.get('1.0','end'))
            self.assertEqual(card['status'],self.card['status'])
            self.assertIn('专用规则',self.service.states['s']['snapshot']['cards'][0]['rule_status'])
            api.assert_called_once()
            dialog.destroy();other.destroy();self.root.update()

    def test_close_pending_window_cancels_timer_and_preserves_lock_until_done(self):
        release=threading.Event()
        def response(config,source,pid):
            release.wait(3)
            raise AnalysisError('模拟失败')
        with patch('autodl_watcher.training_ai_ui.analyze',side_effect=response):
            dialog=self.assistant.explain("s",self.card)
            dialog.start();dialog.destroy()
            self.assertIsNone(dialog.timer)
            self.assertTrue(self.assistant.busy)
            release.set()
            deadline=time.monotonic()+2
            while self.assistant.busy and time.monotonic()<deadline:
                self.assistant.poll();time.sleep(.01)
            self.root.update()
            self.assertFalse(self.assistant.busy)


if __name__=='__main__':
    unittest.main()
