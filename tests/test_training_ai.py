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

CONFIG = {"base_url": "https://example.com/v1", "protocol": "responses", "model": "example-model"}
SOURCE = "Epoch 2 Dice=0.81 custom_quality=4.2"
RESULT = {"summary": "日志显示第2轮，Dice为0.81。", "metrics": [{"name": "Dice", "value": "0.81", "evidence": "Dice=0.81"}]}


def sse(event):
    return b"data: " + json.dumps(event).encode()


def completed():
    return sse({"type": "response.completed", "response": {"status": "completed", "output": [{"content": [{"type": "output_text", "text": json.dumps(RESULT)}]}]}})


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
        self.assertEqual(result['metrics'],RESULT['metrics'])
        self.assertFalse(call.kwargs['allow_redirects'])
        self.assertFalse(call.kwargs['json']['store'])
        self.assertTrue(call.kwargs['stream'])
        self.assertNotIn('test-secret-value',json.dumps(call.kwargs['json']))

    def test_chat_stop_and_done_required(self):
        lines=[sse({'choices':[{'index':0,'delta':{'content':json.dumps(RESULT)},'finish_reason':None}]}),sse({'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}),b'data: [DONE]']
        result,call=self.call(lines,{**CONFIG,'protocol':'chat'})
        self.assertEqual(result['summary'],RESULT['summary'])
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
            self.assertEqual(json.loads(path.read_text()),CONFIG)
            save.assert_called_once_with(CONFIG['base_url'],'test-key')
            self.assertTrue(ai_credentials._target(CONFIG['base_url']).startswith('autodl-watcher/'))
            self.assertNotEqual(ai_credentials._target(CONFIG['base_url']),ai_credentials._target('https://other.example/v1'))

    def test_unproven_metrics_rejected_and_no_model_state_used(self):
        with self.assertRaisesRegex(AnalysisError,'原文证据'):
            decode_result(json.dumps({'summary':'x','metrics':[{'name':'loss','value':'.01','evidence':'loss=.01'}]}),SOURCE)
        result=decode_result(json.dumps({**RESULT,'status':'completed','power_on':True}),SOURCE)
        self.assertNotIn('status',result)
        self.assertNotIn('power_on',result)

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
        self.assistant=AIAssistant(self.root,self.path)

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

    def test_manual_only_single_request_and_main_thread_result(self):
        started,release=threading.Event(),threading.Event()
        def response(config,source):
            started.set();release.wait(3)
            return {**RESULT,'model':'example-model','analyzed_at':time.time()}
        card={'name':'solver.py','pid':10,'details':SOURCE,'status':'进程存活'}
        with patch('autodl_watcher.training_ai_ui.analyze',side_effect=response) as api:
            dialog=self.assistant.explain(card)
            self.root.update();api.assert_not_called()
            dialog.start();self.assertTrue(started.wait(1))
            other=self.assistant.explain(card);other.start()
            self.assertIn('已有 AI 请求',other.status.cget('text'))
            release.set()
            deadline=time.monotonic()+3
            while str(dialog.send.cget('state'))=='disabled' and time.monotonic()<deadline:
                self.root.update();time.sleep(.02)
            self.assertIn('Dice',dialog.output.get('1.0','end'))
            self.assertEqual(card['status'],'进程存活')
            api.assert_called_once()
            dialog.destroy();other.destroy();self.root.update()

    def test_close_pending_window_cancels_timer_and_preserves_lock_until_done(self):
        release=threading.Event()
        def response(config,source):
            release.wait(3)
            raise AnalysisError('模拟失败')
        with patch('autodl_watcher.training_ai_ui.analyze',side_effect=response):
            dialog=self.assistant.explain({'name':'solver','pid':10,'details':SOURCE})
            dialog.start();dialog.destroy()
            self.assertIsNone(dialog.timer)
            self.assertTrue(self.assistant.lock.locked())
            release.set()
            deadline=time.monotonic()+2
            while self.assistant.lock.locked() and time.monotonic()<deadline:
                time.sleep(.01)
            self.root.update()
            self.assertFalse(self.assistant.lock.locked())


if __name__=='__main__':
    unittest.main()
