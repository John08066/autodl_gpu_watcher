import unittest
from autodl_watcher.training import parse_progress, TrainingServer, TrainingTracker
from autodl_watcher.training_ui import metrics_text
from test_training import snapshot

class GeneralProgressTest(unittest.TestCase):
    def test_event_top_level_metrics_are_not_discarded(self):
        p=parse_progress('{"event":"train","global_step":4,"Dice":0.9,"loss":0.1}')
        self.assertEqual(p['train']['metrics'],{'Dice':0.9,'loss':0.1})
        self.assertEqual(p['step'],4)

    def test_validation_epoch_fraction_does_not_override_phase(self):
        p=parse_progress('validation Epoch 3/10 mAP=0.81')
        self.assertEqual(p['phase'],'test')

    def test_nonstandard_script_with_generic_log_is_discovered(self):
        from autodl_watcher.remote_probe import discover_evidence, training_roots
        from autodl_watcher.training import DEFAULT_SCRIPTS
        from unittest.mock import patch
        process=dict(pid=1,ppid=0,args=['python','solver.py'],cwd='/project')
        with patch('autodl_watcher.remote_probe.task_files',return_value=['/project/result.txt']), patch('autodl_watcher.remote_probe.read_log',return_value={'head':"{'epoch': 1.5, 'loss': 0.2}",'tail':''}):
            discover_evidence([process],'/project',{},set())
        self.assertEqual(training_roots([process],DEFAULT_SCRIPTS.split(','),'/project'),[process])

    def test_custom_json_metrics_and_nested_names(self):
        p=parse_progress('{"phase":"training","epoch":3,"global_step":42,"metrics":{"Dice":0.8,"quality":{"PSNR":31.2}},"pid":10}',expected_pid=10)
        self.assertEqual(p['step'],42)
        self.assertEqual(p['train']['metrics'],{'Dice':0.8,'quality/PSNR':31.2})

    def test_huggingface_python_dictionary_and_small_learning_rate(self):
        p=parse_progress("{'loss': 0.2, 'learning_rate': 2e-06, 'epoch': 1.5}")
        self.assertEqual(p['epoch'],1.5)
        self.assertEqual(p['train']['metrics']['learning_rate'],2e-6)
        self.assertIn('2e-06',metrics_text(p['train']))

    def test_key_value_training_and_validation_keep_different_metrics(self):
        p=parse_progress('Epoch 3/10 train Dice=0.81 IoU:0.73 lr=1e-5\nvalidation epoch=3 step=42 mAP@50=0.91 recall=0.87')
        self.assertEqual(p['train']['metrics']['Dice'],0.81)
        self.assertEqual(p['tests']['validation']['metrics']['mAP@50'],0.91)
        self.assertEqual(p['tests']['validation']['step'],42)

    def test_unattributed_metrics_do_not_claim_training(self):
        p=parse_progress('{"epoch":3,"step":42,"metrics":{"perplexity":12.4}}')
        self.assertEqual(p['phase'],'unknown')
        self.assertEqual(p['observations']['metrics']['perplexity'],12.4)

    def test_batch_trace_and_config_numbers_are_not_metrics(self):
        for text in ['{"step":2,"epoch":1,"indexes":[1,2],"image_sha256":"abc"}', 'batch_size=32 workers=8 seed=2026']:
            p=parse_progress(text)
            self.assertFalse(p['train']);self.assertFalse(p['tests']);self.assertEqual(p['phase'],'unknown')

    def test_other_pid_generic_json_cannot_supply_progress(self):
        p=parse_progress('{"pid":99,"phase":"train","epoch":3,"metrics":{"Dice":0.8}}',expected_pid=10)
        self.assertFalse(p['train']);self.assertIsNone(p['epoch'])

    def test_unknown_output_is_visible_as_uninterpreted_excerpt(self):
        card=TrainingTracker(TrainingServer('s','s','host')).update(snapshot('custom solver: objective improved; iteration is warming up'))['cards'][0]
        self.assertIn('custom solver',card['preview'])
        self.assertEqual(card['level'],'warning')

    def test_tqdm_progress_is_not_fabricated_as_global_step(self):
        p=parse_progress('Epoch 2: 45%|####      | 45/100 [00:04<00:05, 9it/s, custom_score=0.73]')
        self.assertIsNone(p['step'])
        self.assertEqual(p['train']['metrics']['custom_score'],0.73)

if __name__=='__main__':unittest.main()
