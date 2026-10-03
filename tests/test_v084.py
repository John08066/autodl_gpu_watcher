import queue
import sys
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from autodl_watcher.training import TrainingServer, TrainingConnection
from autodl_watcher.training_ui import TrainingService
from test_training import snapshot


# 真正的本地子进程验证管道生命周期；不会连接远端或发起失败登录。
CHILD = '''import sys,json,time
json.loads(sys.stdin.readline())
for line in sys.stdin:
 request=json.loads(line)
 if request.get("previous")==["hang"]:time.sleep(15)
 if request.get("previous")==["drop"]:sys.exit(1)
 print(json.dumps({"tasks":[],"gpus":[],"echo":request.get("previous")}),flush=True)
'''

class PersistentSSHTest(unittest.TestCase):
    def setUp(self):
        self.server=TrainingServer('s','203','host')
        self.command=patch.object(TrainingConnection,'_command',return_value=[sys.executable,'-u','-c',CHILD])
        self.command.start()
        self.addCleanup(self.command.stop)
        self.connection=TrainingConnection(self.server)
        self.addCleanup(self.connection.close)

    def test_missing_probe_does_not_leave_an_authenticated_idle_process(self):
        with patch('autodl_watcher.training.Path.read_text',side_effect=FileNotFoundError('probe')), \
             patch('autodl_watcher.training.subprocess.Popen') as spawn:
            with self.assertRaises(FileNotFoundError):self.connection.collect()
        self.assertFalse(spawn.called)
        self.assertEqual(self.connection.attempts,0)

    def test_samples_share_one_process_and_are_not_cached(self):
        first=self.connection.collect(['first'])
        pid=self.connection.process.pid
        second=self.connection.collect(['second'])
        self.assertEqual(first['echo'],['first'])
        self.assertEqual(second['echo'],['second'])
        self.assertEqual(self.connection.process.pid,pid)
        self.assertEqual((self.connection.attempts,self.connection.samples),(1,2))
        process=self.connection.process
        self.connection.close()
        self.assertIsNotNone(process.poll())
        self.assertIsNone(self.connection.process)
        with self.assertRaises(OSError):self.connection.collect()

    def test_remote_disconnect_is_not_retried_inside_sample(self):
        self.connection.collect()
        with self.assertRaises(OSError):self.connection.collect(['drop'])
        self.assertEqual(self.connection.attempts,1)
        self.assertIsNone(self.connection.process)
        self.connection.collect()
        self.assertEqual((self.connection.attempts,self.connection.samples),(2,2))

    def test_timeout_discards_old_pipe_and_next_response_is_fresh(self):
        self.connection.collect()
        with patch.object(TrainingConnection,'TIMEOUT',.15):
            with self.assertRaisesRegex(OSError,'超时'):self.connection.collect(['hang'])
        self.assertIsNone(self.connection.process)
        self.assertEqual(self.connection.collect(['fresh'])['echo'],['fresh'])

    def test_stop_interrupts_pending_read_without_another_connection(self):
        self.connection.collect()
        finished=threading.Event()
        def sample():
            try:self.connection.collect(['hang'])
            except OSError:pass
            finally:finished.set()
        thread=threading.Thread(target=sample);thread.start();time.sleep(.1)
        self.connection.close()
        self.assertTrue(finished.wait(3));thread.join()
        self.assertEqual(self.connection.attempts,1)

    def test_maxstartups_is_not_hidden_by_last_stderr_line(self):
        script="import sys;sys.stderr.write('kex_exchange_identification: banner line 0: Exceeded MaxStartups\\nConnection closed by host port 22\\n');sys.exit(255)"
        with patch.object(TrainingConnection,'_command',return_value=[sys.executable,'-u','-c',script]):
            with self.assertRaisesRegex(OSError,'MaxStartups'):self.connection.collect()

    def test_publickey_and_host_identity_failures_are_distinguished(self):
        for message, expected in [('Permission denied (publickey).','公钥认证失败'),
                                  ('Host key verification failed.','主机身份核验失败'),
                                  ('Connection timed out','连接超时')]:
            with self.subTest(message=message):
                script='import sys;sys.stderr.write('+repr(message+'\n')+');sys.exit(255)'
                with patch.object(TrainingConnection,'_command',return_value=[sys.executable,'-u','-c',script]):
                    with self.assertRaisesRegex(OSError,expected):self.connection.collect()
                self.assertIsNone(self.connection.process)

    def test_invalid_response_closes_session_instead_of_using_stale_data(self):
        script="import sys;sys.stdin.readline();sys.stdin.readline();print('{}',flush=True)"
        with patch.object(TrainingConnection,'_command',return_value=[sys.executable,'-u','-c',script]):
            with self.assertRaises(ValueError):self.connection.collect()
        self.assertIsNone(self.connection.process)
        self.assertEqual(self.connection.samples,0)
        self.assertEqual(self.connection.collect(['recovered'])['echo'],['recovered'])


class BackoffTest(unittest.TestCase):
    def setUp(self):
        self.service=TrainingService(queue.Queue());self.server=TrainingServer('s','203','host')
        self.service.register(self.server);self.service.start('s',60)
        self.addCleanup(self.service.stop_all)

    def test_backoff_grows_caps_and_does_not_overlap_or_restart_early(self):
        state=self.service.states['s']
        with patch('autodl_watcher.training_ui.time.monotonic',return_value=1000):
            for delay in (60,120,240,300,300):
                self.service.receive(('s',0,None,'SSH接入被限流：MaxStartups'))
                self.assertEqual(state['due'],1000+delay)
            self.service.stop('s');self.service.start('s',60)
            with patch('autodl_watcher.training_ui.threading.Thread') as thread:
                self.service.tick();self.assertFalse(thread.called)
        with patch('autodl_watcher.training_ui.time.monotonic',return_value=1300),patch('autodl_watcher.training_ui.threading.Thread') as thread:
            self.service.tick();self.service.tick();self.assertEqual(thread.call_count,1)
        with patch('autodl_watcher.training_ui.time.monotonic',return_value=1301):
            self.service.receive(('s',state['generation'],snapshot(),''))
            self.assertEqual(state['failures'],0);self.assertEqual(state['due'],1361)

    def test_same_ssh_alias_cannot_run_in_two_pages(self):
        other=replace(self.server,id='other')
        self.service.register(other)
        with self.assertRaisesRegex(ValueError,'SSH'):self.service.start('other',60)

    def test_interval_above_backoff_cap_is_respected(self):
        self.service.stop('s');self.service.start('s',600)
        with patch('autodl_watcher.training_ui.time.monotonic',return_value=10):
            self.service.receive(('s',1,None,'timeout'))
            self.assertEqual(self.service.states['s']['due'],610)

if __name__=='__main__':unittest.main()