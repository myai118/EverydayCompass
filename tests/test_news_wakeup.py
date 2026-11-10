import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import news_wakeup as wake


def stamp(value):
    return wake.timestamp('2026-10-19T' + value + ':00+08:00')


def edition(value='12:50'):
    return {'generated_at': stamp(value).isoformat(), 'test_mode': False,
            **{key: [{'title': 'News story', 'url': 'https://www.channelnewsasia.com/' + key + '/' + str(i)}
                     for i in range(15)] for key in ('singapore', 'world')}}


class FakeGitHub:
    def __init__(self, editions, runs=None):
        self.editions, self.runs, self.posts = editions, runs or [], []

    def edition(self):
        return self.editions.pop(0) if len(self.editions) > 1 else self.editions[0]

    def request(self, path, body=None):
        if body is not None:
            self.posts.append((path, body))
            return {}
        return {'workflow_runs': self.runs}


class WakeupTests(unittest.TestCase):
    def test_missed_main_launch_requests_one_recovery(self):
        client = FakeGitHub([edition('06:50')])
        result = wake.recover(client, stamp('13:08'))
        self.assertEqual(result['action'], 'dispatch')
        self.assertEqual(len(client.posts), 1)
        self.assertEqual(client.posts[0][1]['inputs']['recoveryWindow'], stamp('12:45').isoformat())

    def test_all_three_refresh_windows(self):
        for now, expected in [('07:08', '06:45'), ('13:08', '12:45'), ('19:08', '18:45')]:
            self.assertEqual(wake.latest_window(stamp(now)), stamp(expected))

    def test_midnight_uses_previous_evening(self):
        self.assertEqual(wake.latest_window(stamp('01:00')), stamp('18:45') - dt.timedelta(days=1))

    def test_recovery_waits_five_minutes_for_main_then_launches(self):
        self.assertEqual(wake.decide({}, [], stamp('12:49'))['action'], 'skip')
        self.assertEqual(wake.decide({}, [], stamp('12:53'))['action'], 'dispatch')

    def test_complete_edition_skips(self):
        client = FakeGitHub([edition()])
        self.assertEqual(wake.recover(client, stamp('13:08'))['action'], 'skip')
        self.assertEqual(client.posts, [])

    def test_running_and_queued_news_are_not_duplicated(self):
        for status in wake.ACTIVE:
            self.assertEqual(wake.decide({}, [{'status': status}], stamp('13:08'))['action'], 'skip')

    def test_recovery_cooldown_and_limit(self):
        title = 'News recovery ' + stamp('12:45').isoformat()
        attempt = {'event': 'workflow_dispatch', 'display_title': title,
                   'created_at': stamp('13:08').isoformat(), 'status': 'completed'}
        self.assertEqual(wake.decide({}, [attempt], stamp('13:23'))['action'], 'skip')
        self.assertEqual(wake.decide({}, [attempt], stamp('13:38'))['action'], 'dispatch')
        self.assertEqual(wake.decide({}, [attempt] * 3, stamp('14:08'))['action'], 'skip')

    def test_future_incomplete_or_test_edition_does_not_count(self):
        cases = [edition('18:50'), {**edition(), 'world': []}, {**edition(), 'test_mode': True},
                 {**edition(), 'generated_at': '2026-10-19T12:50:00'},
                 {**edition(), 'world': [{}] * 15}]
        for data in cases:
            self.assertFalse(wake.edition_fresh(data, stamp('12:45'), stamp('13:08')))

    def test_new_completion_between_checks_prevents_dispatch(self):
        client = FakeGitHub([{}, edition()])
        self.assertEqual(wake.recover(client, stamp('13:08'))['action'], 'skip')
        self.assertEqual(client.posts, [])

    def test_dry_run_never_posts(self):
        client = FakeGitHub([{}])
        self.assertEqual(wake.recover(client, stamp('13:08'), dry_run=True)['action'], 'would_dispatch')
        self.assertEqual(client.posts, [])


class WorkflowRunnerTests(unittest.TestCase):
    def test_actual_gate_as_external_python_file(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/daily-news.yml').read_text()
        body = workflow.split('        shell: python\n        run: |\n', 1)[1]
        body = textwrap.dedent(body.split('\n      - name:', 1)[0])
        with tempfile.TemporaryDirectory() as temporary:
            runner = Path(temporary)
            workspace = runner / 'workspace'
            (workspace / 'scripts').mkdir(parents=True)
            (workspace / 'data').mkdir()
            (workspace / 'scripts/news_wakeup.py').write_text(Path(wake.__file__).read_text())
            data = edition()
            data['generated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            (workspace / 'data/daily-news.json').write_text(json.dumps(data))
            # GitHub runs shell: python using a file outside the checkout.
            script = runner / 'github-temporary-step.py'
            script.write_text(body)
            for event, recovery, expected in [('schedule', '', 'false'),
                                               ('workflow_dispatch', '', 'true'),
                                               ('workflow_dispatch', 'recovery-test', 'false')]:
                output = runner / 'output.txt'
                output.write_text('')
                env = {**os.environ, 'GITHUB_WORKSPACE': str(workspace),
                       'GITHUB_OUTPUT': str(output), 'GITHUB_EVENT_NAME': event,
                       'RECOVERY_WINDOW': recovery}
                result = subprocess.run([sys.executable, '-I', str(script)],
                                        cwd=workspace, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text().strip(), 'needed=' + expected)


if __name__ == '__main__':
    unittest.main()
