import copy
import unittest
from unittest.mock import patch

from autodl_watcher.remote_probe import discover_evidence, training_roots
from autodl_watcher.training import DEFAULT_SCRIPTS, TrainingServer, TrainingTracker, parse_progress
from test_training import snapshot


class TrainingDiscoveryV085Test(unittest.TestCase):
    def test_non_train_entry_and_module_with_runtime_evidence(self):
        def proc(pid, parent, args, evidence=False, cwd="/project"):
            return dict(pid=pid, ppid=parent, args=args, cwd=cwd, training_candidate=evidence)
        processes = [proc(1,0,["python","experiment.py"],True),
                     proc(2,1,["python","experiment.py"],True),
                     proc(3,0,["python","-m","research.runner"],True),
                     proc(4,0,["python","helper.py"]),
                     proc(5,0,["python","-c","print(1)"],True),
                     proc(6,0,["python","experiment.py"],True,"/other")]
        self.assertEqual([p['pid'] for p in training_roots(processes, DEFAULT_SCRIPTS.split(','), '/project')],[1,3])
        self.assertEqual(training_roots(processes,['only_this.py'],'/project'),[])

    def test_discovery_uses_tmux_ancestry_or_log_evidence_not_just_names(self):
        def proc(pid, parent, args, cwd="/project"):
            return dict(pid=pid, ppid=parent, args=args, cwd=cwd)
        processes=[proc(1,0,["bash"]), proc(2,1,["python","experiment.py"]),
                   proc(3,2,["python","experiment.py"]), proc(4,0,["python","runner.py"]),
                   proc(5,0,["python","idle.py"]), proc(6,1,["python","-m","ipykernel_launcher"]),
                   proc(7,1,["python","other.py"],"/other")]
        def files(item):
            return ['/project/metrics.jsonl'] if item['pid']==4 else []
        with patch('autodl_watcher.remote_probe.task_files',side_effect=files) as paths, \
             patch('autodl_watcher.remote_probe.read_log',return_value={'head':'{"event":"train","epoch":2}', 'tail':''}):
            discover_evidence(processes,'/project',{1:'experiment-session'},set())
        roots=training_roots(processes,DEFAULT_SCRIPTS.split(','),'/project')
        self.assertEqual([p['pid'] for p in roots],[2,4])
        self.assertEqual(roots[0]['tmux_session'],'experiment-session')
        self.assertEqual(paths.call_count,3)

    def test_q10_plural_steps_and_epoch_validation(self):
        text='TRAIN {"pid":10,"epoch":9,"steps":15200,"steps_total":21576,"loss":0.07}\n'
        text+="EPOCH_DONE joint 9 16182 source_best 9 val {'auc':0.98,'ce':0.15}"
        p=parse_progress(text,expected_pid=10)
        self.assertEqual((p['epoch'],p['step'],p['total_steps']),(9,16182,21576))
        self.assertEqual(p['tests']['joint/validation']['metrics'],{'auc':0.98,'ce':0.15})
        self.assertFalse(p['completed'])
        other=parse_progress(text,expected_pid=99)
        self.assertFalse(other['tests'])

    def test_raw_details_contain_one_training_log_without_batch_trace(self):
        value=snapshot('TRAIN {"pid":10,"epoch":9,"steps":15200,"loss":0.07}\n')
        log=value['tasks'][0]['logs'][0]
        trace=dict(log,path='/project/batch_trace.jsonl',head='{"indexes":[1,2],"image_sha256":"trace-only"}',modified_at=log['modified_at']+1)
        value['tasks'][0]['logs'].append(trace)
        card=TrainingTracker(TrainingServer('s','s','host')).update(value)['cards'][0]
        self.assertEqual(card['details'],log['head'])
        self.assertEqual(card['log_path'],'/project/training.log')
        self.assertNotIn('trace-only',card['details'])

    def test_restart_keeps_old_run_as_history_not_current_failure(self):
        tracker=TrainingTracker(TrainingServer('s','s','host'))
        old=snapshot();tracker.update(old)
        new=copy.deepcopy(old['tasks'][0]);new.update(key='boot:11:200',pid=11,started_at=new['started_at']+10)
        old['tasks'][0]['alive']=False;old['tasks'].append(new)
        cards=tracker.update(old)['cards']
        self.assertFalse(cards[0].get('history',False))
        self.assertTrue(cards[1]['history'])
        self.assertIn('历史',cards[1]['status'])

    def test_different_log_same_name_is_not_a_restart(self):
        value=snapshot();other=copy.deepcopy(value['tasks'][0])
        other.update(key='boot:11:200',pid=11,started_at=other['started_at']+10)
        other['logs'][0]['path']='/project/other.log'
        value['tasks'][0]['alive']=False;value['tasks'].append(other)
        cards=TrainingTracker(TrainingServer('s','s','host')).update(value)['cards']
        self.assertFalse(cards[1].get('history',False))
        self.assertEqual(cards[1]['level'],'error')


if __name__=='__main__':
    unittest.main()
