import json
import queue
import tempfile
import time
from pathlib import Path
import unittest
from unittest.mock import patch

from autodl_watcher.remote_probe import training_roots
from autodl_watcher.training import TrainingServer, TrainingTracker, collect_remote, load_servers, log_text, parse_progress, save_servers
from autodl_watcher.training_ui import TrainingService


LOG = """nEpochs: 30
start_epoch: 0
2026-09-30 20:00:00 - INFO - ===> Epoch[1] start!
2026-09-30 20:01:00 - INFO - Iter: 2398 training-loss, overall: 0.2
2026-09-30 20:01:00 - INFO - Iter: 2398 training-metric, acc: 0.9 training-metric, auc: 0.97
2026-09-30 20:02:00 - INFO - ===> Test start!
2026-09-30 20:03:00 - INFO - dataset: CDF step: 2696 testing-loss, overall: 0.4
2026-09-30 20:03:00 - INFO - dataset: CDF step: 2696 testing-metric, auc: 0.88 testing-metric, acc: 0.78
"""


def snapshot(text=LOG, alive=True, now=1790769960):
    return dict(observed_at=now, utc_offset="+0800", scan_ok=True, gpus=[], user="me", tasks=[dict(
        key="boot:10:100", pid=10, name="experiment", started_at=now-600, user="me", cwd="/project", alive=alive,
        gpu_indices=[], gpu_memory_mb=None, logs=[dict(path="/project/training.log", head=text, tail="", modified_at=now-30, size=len(text))])])


class TrainingTest(unittest.TestCase):
    def setUp(self):
        self.server = TrainingServer("s", "203-1", "server", project="/project")

    def test_roots_exclude_workers_wrappers_other_users_and_outside_project(self):
        def proc(pid, parent, args, cwd="/project"):
            return dict(pid=pid, ppid=parent, args=args, cwd=cwd)
        processes = [proc(1, 0, ["timeout", "30", "python", "train.py"]), proc(2, 1, ["python", "-u", "train.py"]),
                     proc(3, 2, ["python", "-u", "train.py"]), proc(4, 0, ["python", "other.py", "train.py"]),
                     proc(5, 0, ["python", "train.py"], "/project-other"), proc(6, 0, ["python", "kernel.py"]),
                     proc(7, 0, ["python3.11", "/project/train.py"], "/project/run")]
        self.assertEqual([item["pid"] for item in training_roots(processes, ["train.py"], "/project")], [2, 7])

    def test_epoch_origin_and_metric_epoch_step_are_preserved(self):
        result = parse_progress(LOG + "2026-09-30 20:04:00 - ===> Epoch[2] start!\n", "+0800")
        self.assertEqual((result["epoch"], result["raw_epoch"], result["total_epochs"]), (3, 2, 31))
        self.assertEqual(result["train"]["epoch"], 2)
        self.assertEqual(result["train"]["step"], 2398)
        self.assertEqual(result["tests"]["CDF"]["epoch"], 2)
        self.assertEqual(result["tests"]["CDF"]["metrics"], {"loss": .4, "auc": .88, "acc": .78})
        self.assertEqual(result["phase"], "train")

    def test_best_metric_summary_does_not_replace_latest_metrics(self):
        result = parse_progress(LOG + "Average best metric\n| CDF auc: 0.99 |\n")
        self.assertEqual(result["tests"]["CDF"]["metrics"]["auc"], .88)

    def test_test_done_is_not_training_completed(self):
        result = parse_progress(LOG + "===> Test Done!\n")
        self.assertFalse(result["completed"])
        self.assertEqual(result["phase"], "train")

    def test_explicit_completion_and_oom_are_distinct(self):
        result = parse_progress(LOG + "RuntimeError: CUDA out of memory\n")
        self.assertIn("out of memory", result["error"])
        self.assertFalse(result["completed"])
        result = parse_progress(LOG + "Stop Training on best Testing metric AUC .88\n")
        self.assertTrue(result["completed"])
        self.assertFalse(result["error"])

    def test_old_oom_cleared_by_later_training_progress(self):
        result = parse_progress("RuntimeError: CUDA out of memory\n" + LOG)
        self.assertFalse(result["error"])

    def test_jsonl_protocol_supports_other_training_frameworks(self):
        result = parse_progress(json.dumps(dict(event="train", epoch=8, total_epochs=20, step=120, metrics={"loss": .2})) + "\n" +
                                json.dumps(dict(event="test", epoch=8, dataset="validation", metrics={"auc": .91})))
        self.assertEqual((result["epoch"], result["total_epochs"]), (8, 20))
        self.assertEqual(result["tests"]["validation"]["metrics"]["auc"], .91)
        self.assertEqual(result["phase"], "test")

    def test_gap_does_not_attach_new_metrics_to_old_header_epoch(self):
        log = dict(head="nEpochs: 30\nstart_epoch: 0\n===> Epoch[0] start!\n", gap=True,
                   tail="Iter: 9999 training-metric, auc: 0.9\n")
        result = parse_progress(log_text(log))
        self.assertIsNone(result["epoch"])
        self.assertIsNone(result["train"]["epoch"])
        self.assertEqual(result["total_epochs"], 31)

    def test_missing_metric_is_not_zero(self):
        result = parse_progress("Epoch 3/10\n")
        self.assertEqual(result["train"], {})
        self.assertEqual(result["tests"], {})

    def test_stale_progress_is_warning_and_never_deadlock_claim(self):
        tracker = TrainingTracker(self.server)
        first = tracker.update(snapshot(now=1790769960))
        self.assertEqual(first["cards"][0]["level"], "success")
        later = tracker.update(snapshot(now=1790769960+9000))
        self.assertEqual(later["cards"][0]["level"], "warning")
        self.assertNotIn("死锁", later["cards"][0]["status"])

    def test_reprinting_same_step_with_new_timestamp_is_not_progress(self):
        tracker = TrainingTracker(self.server)
        first = tracker.update(snapshot())["cards"][0]
        repeated = LOG.replace("20:03:00", "23:03:00")
        later = tracker.update(snapshot(repeated, now=1790769960+11000))["cards"][0]
        self.assertEqual(later["advanced_at"], first["advanced_at"])
        self.assertEqual(later["level"], "warning")

    def test_process_exit_requires_error_or_completion_evidence(self):
        for suffix, level, label in [("", "error", "未确认正常完成"), ("RuntimeError: CUDA out of memory", "error", "异常中止"),
                                     ("Stop Training on best Testing metric .9", "idle", "已完成")]:
            with self.subTest(level=level):
                card = TrainingTracker(self.server).update(snapshot(LOG+suffix, False))["cards"][0]
                self.assertEqual(card["level"], level)
                self.assertIn(label, card["status"])

    def test_alive_error_is_not_falsely_reported_as_exited(self):
        card = TrainingTracker(self.server).update(snapshot(LOG+"RuntimeError: CUDA out of memory", True))["cards"][0]
        self.assertEqual(card["level"], "error")
        self.assertIn("仍存活", card["status"])

    def test_reused_pid_does_not_inherit_old_progress(self):
        tracker=TrainingTracker(self.server)
        tracker.update(snapshot())
        newer=snapshot("",True)
        newer["tasks"][0]["key"]="boot:10:200"
        newer["tasks"][0]["logs"]=[]
        card=tracker.update(newer)["cards"][0]
        self.assertIsNone(card["progress"]["epoch"])
        self.assertEqual(card["level"],"warning")

    def test_partial_scan_retains_uncertain_tasks(self):
        tracker=TrainingTracker(self.server)
        tracker.update(snapshot())
        current=snapshot();current.update(tasks=[],scan_ok=False)
        card=tracker.update(current)["cards"][0]
        self.assertEqual(card["level"],"warning")
        self.assertIn("列表不完整",card["status"])

    def test_local_profiles_are_atomic_and_reject_bad_input(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'servers.json'
            save_servers(path,[self.server])
            self.assertEqual(load_servers(path),[self.server])
            for alias in ('-oProxyCommand=x','server\ncommand','a b'):
                with self.assertRaises(ValueError):
                    TrainingServer('s','s',alias).validate()
            with self.assertRaises(ValueError):
                TrainingServer('s','s','host',stalled_minutes=float('nan')).validate()

    def test_ssh_uses_no_forwarding_no_prompts_and_only_stdin_probe(self):
        with patch('autodl_watcher.training.ssh_executable',return_value='ssh'),patch('autodl_watcher.training.subprocess.run') as run:
            run.return_value.returncode=0;run.return_value.stdout=json.dumps(snapshot())
            collect_remote(self.server)
            args=run.call_args.args[0]
            self.assertIn('ClearAllForwardings=yes',args)
            self.assertIn('StrictHostKeyChecking=yes',args)
            self.assertIn('BatchMode=yes',args)
            self.assertNotIn('shell',run.call_args.kwargs)
            self.assertLessEqual(run.call_args.kwargs['timeout'],35)

    def test_connection_failure_preserves_last_snapshot_as_unknown(self):
        service=TrainingService(queue.Queue());service.register(self.server);service.start('s',60)
        service.receive(('s',0,snapshot(),''))
        service.receive(('s',0,None,'SSH timeout'))
        state=service.states['s']
        self.assertEqual(state['snapshot']['error'],'SSH timeout')
        self.assertEqual(len(state['snapshot']['cards']),1)

    def test_late_response_after_stop_cannot_reactivate_monitor(self):
        service=TrainingService(queue.Queue());service.register(self.server);service.start('s',60)
        service.states['s']['busy']=True
        service.stop('s');service.receive(('s',0,snapshot(),''))
        self.assertFalse(service.states['s']['active'])
        self.assertFalse(service.busy())
        self.assertIsNone(service.states['s']['snapshot'])

    def test_only_one_request_per_server_and_independent_sources(self):
        service=TrainingService(queue.Queue());service.register(self.server)
        other=TrainingServer('other','4090','ssh4090');service.register(other)
        service.start('s',60);service.start('other',60)
        with patch('autodl_watcher.training_ui.threading.Thread') as thread:
            service.tick();service.tick()
            self.assertEqual(thread.call_count,2)
        service.stop('s')
        self.assertTrue(service.states['other']['active'])


if __name__=='__main__':
    unittest.main()
