from dataclasses import asdict
from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path

from autodl_watcher.remote_probe import training_roots
from autodl_watcher.training import TrainingServer, TrainingTracker, load_servers, parse_progress
from autodl_watcher.training_ui import metrics_text, task_memory
from test_training import snapshot


TRAIN = '2026-10-03T17:27:25.652329+08:00 TRAIN {"status":"running","pid":10,"epoch":0,"steps_per_arm":100,"loss":{"paired":1.0937,"rolled":1.0868},"peak_gpu_allocated":16532455424}'
NOW = datetime.fromisoformat('2026-10-03T17:28:00+08:00').timestamp()


class TrainingCompatibilityTest(unittest.TestCase):
    def test_default_patterns_recognize_new_names_without_workers_or_launchers(self):
        def proc(pid, parent, args, cwd='/project'):
            return dict(pid=pid,ppid=parent,args=args,cwd=cwd)
        processes = [proc(1,0,['python','-u','scripts/train_p03_t01.py']),
                     proc(2,1,['python','-u','scripts/train_p03_t01.py']),
                     proc(3,0,['python3','train_next_experiment.py']),
                     proc(4,0,['python','kernel.py','train_fake.py']),
                     proc(5,0,['python','-m','jupyter','train_fake.py']),
                     proc(6,0,['python','-c','pass','train_fake.py']),
                     proc(7,0,['python','train_p04.py'],'/project-other'),
                     proc(8,0,['python','trainer.py'])]
        patterns=TrainingServer('s','s','server').scripts.split(',')
        self.assertEqual([p['pid'] for p in training_roots(processes,patterns,'/project')],[1,3,8])
        self.assertEqual([p['pid'] for p in training_roots(processes,['trainer.py'],'/project')],[8])

    def test_legacy_default_migrates_in_memory_but_custom_filter_and_file_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'servers.json'
            old=asdict(TrainingServer('old','old','host',scripts='train.py,trainer.py,train_net.py'))
            custom=asdict(TrainingServer('custom','custom','host',scripts='only_this.py'))
            path.write_text(json.dumps([old,custom]),encoding='utf-8')
            before=path.read_bytes();servers=load_servers(path)
            self.assertIn('train_*.py',servers[0].scripts)
            self.assertEqual(servers[1].scripts,'only_this.py')
            self.assertEqual(path.read_bytes(),before)

    def test_prefixed_train_keeps_branch_losses_epoch_base_step_and_offset(self):
        result=parse_progress(TRAIN,'+0000',expected_pid=10)
        self.assertEqual((result['phase'],result['epoch'],result['raw_epoch'],result['step']),('train',1,0,100))
        self.assertIsNone(result['total_epochs'])
        self.assertEqual(result['train']['metrics'],{'loss/paired':1.0937,'loss/rolled':1.0868})
        self.assertIn('Loss/paired 1.0937',metrics_text(result['train']))
        self.assertEqual(result['peak_memory_bytes'],16532455424)
        self.assertEqual(result['progress_at'],datetime.fromisoformat('2026-10-03T17:27:25.652329+08:00').timestamp())

    def test_secondary_branch_evaluation_preserves_dataset_and_branch(self):
        text=TRAIN+'\n2026-10-03T18:00:00+08:00 EPOCH_END 1 steps_per_arm 3596'
        text+='\n2026-10-03T18:01:00+08:00 EVALUATED paired {"CDF":{"auc":0.8,"ce":0.4},"DFDC":{"auc":0.7,"ce":0.6}}'
        text+='\n2026-10-03T18:02:00+08:00 EVALUATED rolled {"CDF":{"auc":0.6,"ce":0.8}}'
        result=parse_progress(text,expected_pid=10)
        self.assertEqual(result['phase'],'test');self.assertFalse(result['completed'])
        self.assertEqual(result['tests']['paired/CDF']['metrics'],{'auc':0.8,'ce':0.4})
        self.assertEqual(result['tests']['rolled/CDF']['metrics'],{'auc':0.6,'ce':0.8})
        self.assertEqual(result['tests']['paired/CDF']['step'],3596)
        self.assertEqual(result['tests']['paired/CDF']['epoch'],2)
        self.assertEqual(result['train']['step'],100)
        self.assertIn('CE 0.4000',metrics_text(result['tests']['paired/CDF']))

    def test_completion_error_and_epoch_end_are_distinct(self):
        for suffix,completed,phase in [('EPOCH_END 0 steps_per_arm 1798',False,'between'),
                                       ('TRAINING_COMPLETE {"steps_per_arm":3596}',True,'train'),
                                       ('SMOKE_PASS {"steps_per_arm":2}',True,'train')]:
            with self.subTest(suffix=suffix):
                result=parse_progress(TRAIN+'\n2026-10-03T17:28:00+08:00 '+suffix,expected_pid=10)
                self.assertEqual(result['completed'],completed);self.assertEqual(result['phase'],phase)
        result=parse_progress(TRAIN+'\nTRAINING_FAILED {"message":"CUDA error"}',expected_pid=10)
        self.assertEqual(result['error'],'CUDA error');self.assertFalse(result['completed'])

    def test_bad_json_unknown_trace_and_nonfinite_metrics(self):
        result=parse_progress('TRAIN {bad}\n'+TRAIN+'\n{"step":132,"epoch":0,"indexes":[1,2]}',expected_pid=10)
        self.assertEqual(result['step'],100)
        for value in ('NaN','Infinity','-Infinity'):
            with self.subTest(value=value):
                text=TRAIN.replace('1.0937',value)
                result=parse_progress(text,expected_pid=10)
                self.assertTrue(result['error']);self.assertNotIn('loss/paired',result['train']['metrics'])

    def test_pid_mismatch_cannot_supply_progress_or_task_memory(self):
        result=parse_progress(TRAIN,expected_pid=999)
        self.assertEqual(result['phase'],'unknown');self.assertIsNone(result['epoch'])
        self.assertIsNone(result.get('peak_memory_bytes'))

    def test_peak_memory_is_separate_from_current_gpu_memory(self):
        progress=parse_progress(TRAIN,expected_pid=10)
        value=task_memory(dict(alive=True,gpu_memory_mb=None,progress=progress))
        self.assertIn('峰值',value);self.assertIn('15.40 GiB',value);self.assertIn('日志',value)
        self.assertEqual(task_memory(dict(alive=True,gpu_memory_mb=4096,progress=progress)),'显存 4.00 GiB')
        self.assertIn('已退出',task_memory(dict(alive=False,gpu_memory_mb=None,progress=progress)))

    def test_runner_is_selected_instead_of_unrecognized_batch_trace(self):
        value=snapshot(TRAIN,now=NOW)
        runner=value['tasks'][0]['logs'][0];runner['path']='/project/train_runner.log'
        trace=dict(runner,path='/project/batch_trace.jsonl',head='{"step":134,"epoch":0}',modified_at=NOW)
        value['tasks'][0]['logs']=[trace,runner]
        card=TrainingTracker(TrainingServer('s','s','host')).update(value)['cards'][0]
        self.assertEqual(card['status'],'正在训练')
        self.assertEqual(card['progress']['step'],100)
        self.assertIn('loss/rolled',card['progress']['train']['metrics'])


if __name__=='__main__':
    unittest.main()
