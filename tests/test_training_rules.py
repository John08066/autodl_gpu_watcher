import copy
import json
from pathlib import Path
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

from autodl_watcher.training import TrainingTracker, TrainingServer
from autodl_watcher.training_ai import AnalysisError
from autodl_watcher.training_rules import validate_profile, parse_rules, RuleStore, task_identity
from autodl_watcher.training_ui import TrainingService
from autodl_watcher.training_ai_ui import AIAssistant
from test_training import snapshot

LINE='ITER {"pid":10,"round":2,"count":40,"stats":{"Dice":0.81,"PSNR":32.1}}'
PROFILE={'name':'solver专用','rules':[{'format':'json','contains':'ITER ', 'phase':'train','fields':{'epoch':'round','step':'count','pid':'pid'},'metrics_path':'stats','example':LINE}]}


class RuleEngineTest(unittest.TestCase):
    def test_future_values_and_new_metrics_are_extracted_not_frozen(self):
        profile=validate_profile(PROFILE,LINE,10)
        updated='ITER {"pid":10,"round":3,"count":80,"stats":{"Dice":0.9,"PSNR":34,"new_metric":0.5}}'
        event=parse_rules(profile,updated,10)[0]
        self.assertEqual((event['epoch'],event['step']),(3,80))
        self.assertEqual(event['metrics']['new_metric'],.5)
        self.assertEqual(parse_rules(profile,updated,99),[])

    def test_text_template_test_phase_and_branch(self):
        line="EPOCH_DONE joint 9 16182 source_best 9 val {'auc':0.98,'ce':0.2}"
        rule={'format':'text','template':'EPOCH_DONE {arm:word} {epoch:number} {step:number} source_best {best:number} val {scores:json}', 'phase':'test','fields':{'epoch':'epoch','step':'step','dataset':'arm'},'metrics_path':'scores','example':line}
        value=validate_profile({'name':'q10','rules':[rule]},line)
        event=parse_rules(value,line.replace('9 16182','10 18000').replace('0.98','0.99'),10)[0]
        self.assertEqual(event['step'],18000)
        self.assertEqual(event['metrics']['auc'],.99)
        self.assertEqual(event['dataset'],'joint')

    def test_shared_log_does_not_borrow_another_pid_validation(self):
        line="EPOCH_DONE joint 9 16182 source_best 9 val {'auc':0.98}"
        rule={'format':'text','template':'EPOCH_DONE {arm:word} {epoch:number} {step:number} source_best {best:number} val {scores:json}', 'phase':'test','fields':{'epoch':'epoch','step':'step','dataset':'arm'},'metrics_path':'scores','example':line}
        text=LINE+'\n'+ 'TRAIN {"pid":99,"step":5}'+'\n'+line
        events=parse_rules({'rules':[PROFILE['rules'][0],rule]},text,10)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['phase'],'train')

    def test_bad_examples_constants_and_commands_rejected(self):
        for change in [{'example':'made up'},{'fields':{'epoch':2}},{'command':'ls'},{'phase':'completed'}]:
            p=copy.deepcopy(PROFILE);p['rules'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_profile(p,LINE,10)
        with self.assertRaises(ValueError):validate_profile(PROFILE,LINE,99)

    def test_tracker_applies_fresh_metrics_preserves_errors_and_falls_back(self):
        entry={'profile':PROFILE,'log_path':'/project/training.log'}
        tracker=TrainingTracker(TrainingServer('s','s','host'),lambda task:entry)
        card=tracker.update(snapshot(LINE))['cards'][0]
        self.assertEqual(card['progress']['train']['metrics']['Dice'],.81)
        self.assertIn('专用规则已启用',card['rule_status'])
        card=tracker.update(snapshot('Epoch 1/20 loss=0.2\n' + LINE))['cards'][0]
        self.assertNotIn('最新未覆盖行',card['rule_status'])
        later=snapshot(LINE.replace('40','80').replace('0.81','0.92'))
        card=tracker.update(later)['cards'][0]
        self.assertEqual(card['progress']['step'],80)
        self.assertEqual(card['progress']['train']['metrics']['Dice'],.92)
        card=tracker.update(snapshot('some new format, nothing matches'))['cards'][0]
        self.assertIn('不适用',card['rule_status'])
        self.assertEqual(card['progress']['train'],{})
        card=tracker.update(snapshot(LINE+'\nCUDA out of memory'))['cards'][0]
        self.assertEqual(card['level'],'error')
        self.assertEqual(tracker.update(snapshot(LINE))['cards'][0]['level'],'error')

    def test_adapted_test_names_replace_default_test_names(self):
        text='TRAIN {"pid":10,"epoch":9,"steps":15200,"loss":0.07}\nEPOCH_DONE joint 9 16182 source_best 9 val {"auc":0.98}'
        example=text.splitlines()[1]
        profile={'name':'test','rules':[{'format':'text','template':'EPOCH_DONE {arm:word} {epoch:number} {step:number} source_best {best:number} val {scores:json}', 'phase':'test','fields':{'epoch':'epoch','step':'step','dataset':'arm'},'metrics_path':'scores','example':example}]}
        tracker=TrainingTracker(TrainingServer('s','s','host'),lambda t:{'profile':profile,'log_path':'/project/training.log'})
        card=tracker.update(snapshot(text))['cards'][0]
        self.assertEqual(list(card['progress']['tests']),['joint'])

    def test_same_record_outer_and_inner_timestamps_do_not_reject_rule(self):
        line = '2026-10-07T13:04:52.402788+08:00 TRAIN {"status":"training","time":"2026-10-07T13:04:52.402028+08:00","pid":10,"epoch":10,"steps":17700,"loss":0.0916}'
        profile = {'name':'Q10','rules':[{'format':'json','contains':'TRAIN ', 'phase':'train','fields':{'epoch':'epoch','step':'steps','pid':'pid','time':'time'},'metrics':{'自定义损失':'loss'},'example':line}]}
        tracker = TrainingTracker(TrainingServer('s','s','host'), lambda t: {'profile':profile,'log_path':'/project/training.log'})
        card = tracker.update(snapshot(line))['cards'][0]
        self.assertIn('专用规则已启用', card['rule_status'])
        self.assertEqual(card['progress']['train']['metrics'], {'自定义损失':.0916})
        next_line = line.replace('17700','18000').replace('0.0916','0.08')
        card = tracker.update(snapshot(next_line))['cards'][0]
        self.assertEqual(card['progress']['step'],18000)
        self.assertEqual(card['progress']['train']['metrics'], {'自定义损失':.08})
        # 规则只覆盖TRAIN时，后续测试及未覆盖的新训练格式依然推进，不让旧规则覆盖新进度。
        evaluation = '\n2026-10-07T13:06:00+08:00 EPOCH_DONE joint 10 18200 source_best 10 val {"auc":0.99}'
        card = tracker.update(snapshot(next_line + evaluation))['cards'][0]
        self.assertEqual(card['progress']['phase'],'test')
        self.assertEqual(card['progress']['step'],18200)
        self.assertEqual(card['progress']['train']['metrics'], {'自定义损失':.08})
        self.assertEqual(card['progress']['tests']['joint/validation']['metrics']['auc'],.99)
        self.assertIn('最新未覆盖行使用默认解析',card['rule_status'])
        card = tracker.update(snapshot(next_line + '\nEpoch 11/20 step=19000 loss=0.05'))['cards'][0]
        self.assertEqual(card['progress']['step'],19000)
        self.assertEqual(card['progress']['train']['metrics']['loss'],.05)

    def test_rule_identity_isolated_by_server_and_process_start(self):
        s=TrainingServer('s','s','host')
        task={'key':'boot:10:100'}
        self.assertNotEqual(task_identity(s,task),task_identity(TrainingServer('b','b','other'),task))
        self.assertNotEqual(task_identity(s,task),task_identity(s,{'key':'boot:10:101'}))


class RuleAutomationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'ai.json'
        self.path.write_text(json.dumps({'base_url':'https://example.com/v1','model':'test','auto_generate':True}))
        self.service=TrainingService(queue.Queue())
        self.ai=AIAssistant(None,self.path,self.service);self.service.ai=self.ai
        self.server=TrainingServer('s','s','host');self.service.register(self.server)
        state=self.service.states['s'];state['active']=True;state['snapshot']=state['tracker'].update(snapshot(LINE))

    def tearDown(self):self.temp.cleanup()

    def wait(self):
        deadline=time.monotonic()+3
        while self.ai.busy and time.monotonic()<deadline:
            self.ai.poll();time.sleep(.01)
        self.assertFalse(self.ai.busy)

    def test_auto_once_reuse_after_reload_and_apply_to_card(self):
        with patch('autodl_watcher.training_ai_ui.analyze',return_value={'profile':PROFILE,'service_tier':'flex'}) as api:
            stamp=self.service.states['s']['snapshot']['received_at']
            self.ai.tick();self.wait()
            for _ in range(10):self.ai.tick()
            self.assertEqual(api.call_count,1)
            self.assertEqual(self.service.states['s']['snapshot']['received_at'],stamp)
            self.ai=AIAssistant(None,self.path,self.service);self.service.ai=self.ai
            self.ai.tick();self.assertFalse(self.ai.busy);self.assertEqual(api.call_count,1)
            self.service.reparse('s')
            self.assertIn('专用规则已启用',self.service.states['s']['snapshot']['cards'][0]['rule_status'])

    def test_failure_not_repeated_each_sample_or_restart(self):
        with patch('autodl_watcher.training_ai_ui.analyze',side_effect=AnalysisError('HTTP 429')) as api:
            self.ai.tick();self.wait()
            for _ in range(10):self.ai.tick()
            self.assertEqual(api.call_count,1)
            self.assertIn('429',self.service.states['s']['snapshot']['cards'][0]['rule_status'])
            self.ai=AIAssistant(None,self.path,self.service);self.service.ai=self.ai
            self.ai.tick();self.assertEqual(api.call_count,1)

    def test_global_config_applies_to_new_server_but_rules_do_not_cross(self):
        self.ai.config["prompt"] = "提取新指标FID与所有验证数据集"
        other=TrainingServer('b','4090','other');self.service.register(other)
        state=self.service.states['b'];state['active']=True;state['snapshot']=state['tracker'].update(snapshot(LINE))
        with patch('autodl_watcher.training_ai_ui.analyze',return_value={'profile':PROFILE}) as api:
            self.ai.tick();self.wait();self.ai.tick();self.wait()
            self.assertEqual(api.call_count,2)
            self.assertEqual(api.call_args_list[0].args[0],api.call_args_list[1].args[0])
            self.assertEqual(api.call_args_list[1].args[0]["prompt"],"提取新指标FID与所有验证数据集")
            self.assertEqual(len(self.ai.store.entries),2)

    def test_bad_cache_pauses_auto_and_shows_reason_on_card(self):
        self.path.with_name('training_rules.json').write_text('{bad json')
        self.ai=AIAssistant(None,self.path,self.service);self.service.ai=self.ai
        with patch('autodl_watcher.training_ai_ui.analyze') as api:
            self.ai.tick();api.assert_not_called()
            self.service.reparse('s')
        self.assertIn('规则缓存无法读取',self.service.states['s']['snapshot']['cards'][0]['rule_status'])

    def test_no_auto_without_opt_in_and_no_live_key_read(self):
        self.ai.config['auto_generate']=False
        with patch('autodl_watcher.training_ai_ui.analyze') as api:
            self.ai.tick();api.assert_not_called()


if __name__=='__main__':unittest.main()
