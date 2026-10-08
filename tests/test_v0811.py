import copy
import json
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from autodl_watcher.remote_probe import training_roots
from autodl_watcher.training import DEFAULT_SCRIPTS, TrainingServer
from autodl_watcher.training_ai import analyze, decode_result
from autodl_watcher.training_ai_ui import AIAssistant
from autodl_watcher.training_ui import TrainingService
from test_training import snapshot
from test_training_ai import CONFIG


class TrainingLauncherTest(unittest.TestCase):
    def processes(self):
        def process(pid, parent, script):
            return dict(pid=pid, ppid=parent, key=f"boot:{pid}:100", args=["python",script],
                        cwd="/project", training_candidate=True)
        return [process(1,0,"run_experiment.py"), process(2,1,"train_custom.py"),
                process(3,2,"train_custom.py"), process(4,0,"independent.py")]

    def test_real_trainer_replaces_launcher_and_excludes_dataloader(self):
        roots=training_roots(self.processes(),DEFAULT_SCRIPTS.split(','),'/project')
        self.assertEqual([p['pid'] for p in roots],[2,4])
        self.assertEqual(roots[0]['launcher_keys'],['boot:1:100'])

    def test_parallel_trainers_are_separate_and_custom_scope_is_preserved(self):
        processes=self.processes()
        other=copy.deepcopy(processes[1]);other.update(pid=5,key='boot:5:100',args=['python','train_other.py'])
        processes.append(other)
        self.assertEqual([p['pid'] for p in training_roots(processes,DEFAULT_SCRIPTS.split(','),'/project')],[2,4,5])
        self.assertEqual([p['pid'] for p in training_roots(processes,['run_experiment.py'],'/project')],[1])

    def test_parent_with_own_gpu_work_is_not_discarded(self):
        processes=self.processes()
        processes[0]['gpu_candidate']=True
        roots=training_roots(processes,DEFAULT_SCRIPTS.split(','),'/project')
        self.assertEqual([p['pid'] for p in roots],[1,2,4])
        self.assertEqual(roots[1]['launcher_keys'],[])

    def test_pid_mismatch_is_rejected_before_any_paid_request(self):
        with patch('autodl_watcher.training_ai.ai_credentials.read',return_value='test-key') as key, \
             patch('autodl_watcher.training_ai.requests.Session') as session:
            with self.assertRaisesRegex(ValueError,'PID.*不一致'):
                analyze(CONFIG,'TRAIN {"pid":2,"epoch":1,"steps":100,"loss":0.2}',1)
            key.assert_not_called()
            session.assert_not_called()

    def test_empty_rule_response_explains_fallback(self):
        with self.assertRaisesRegex(ValueError,'空规则'):
            decode_result('{"name":"unavailable","rules":[]}','TRAIN {"pid":2,"epoch":1}',2)

    def test_manual_generation_applies_to_4090_and_next_sample(self):
        line='TRAIN {"pid":2,"epoch":1,"steps":100,"loss":0.2,"aux_loss":0.03}'
        profile={'name':'4090训练规则','rules':[{'format':'json','contains':'TRAIN ', 'phase':'train',
            'fields':{'pid':'pid','epoch':'epoch','step':'steps'},
            'metrics':{'Loss':'loss','辅助损失':'aux_loss'},'example':line}]}
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'ai.json';path.write_text(json.dumps(CONFIG),encoding='utf-8')
            service=TrainingService(queue.Queue());server=TrainingServer('ssh-4090','4090','host')
            service.register(server);ai=AIAssistant(None,path,service);service.ai=ai
            value=snapshot(line);value['tasks'][0].update(pid=2,key='boot:2:100',name='train_custom.py')
            state=service.states[server.id];state['snapshot']=state['tracker'].update(value)
            card=state['snapshot']['cards'][0]
            with patch('autodl_watcher.training_ai_ui.analyze',return_value={'profile':profile}) as api:
                ai.generate(server.id,card,card['details'])
                deadline=time.monotonic()+3
                while ai.busy and time.monotonic()<deadline:
                    ai.poll();time.sleep(.01)
                self.assertFalse(ai.busy)
                self.assertIn('专用规则已启用',state['snapshot']['cards'][0]['rule_status'])
                self.assertEqual(api.call_args.args[2],2)
                next_value=copy.deepcopy(value)
                next_value['tasks'][0]['logs'][0]['head']=line.replace('100','200').replace('0.03','0.04')
                service.receive((server.id,state['generation'],next_value,''))
                current=state['snapshot']['cards'][0]
                self.assertIn('专用规则已启用',current['rule_status'])
                self.assertEqual(current['progress']['step'],200)
                self.assertEqual(current['progress']['train']['metrics']['辅助损失'],0.04)
                api.assert_called_once()
