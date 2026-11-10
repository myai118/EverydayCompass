"""Recover missed Daily News editions using GitHub's existing workflow."""
import base64
import datetime as dt
import json
import os
import urllib.error
import urllib.request

SGT = dt.timezone(dt.timedelta(hours=8))
WORKFLOW = 'daily-news.yml'
GRACE = dt.timedelta(minutes=5)
COOLDOWN = dt.timedelta(minutes=20)
MAX_RECOVERIES = 3
ACTIVE = {'requested', 'waiting', 'pending', 'queued', 'in_progress'}


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return result


def latest_window(now):
    local = now.astimezone(SGT)
    windows = [local.replace(hour=h, minute=45, second=0, microsecond=0)
               for h in (6, 12, 18)]
    return max([w for w in windows if w <= local] or
               [windows[-1] - dt.timedelta(days=1)])


def edition_fresh(edition, target, now):
    try:
        generated = timestamp(edition['generated_at'])
        return (target <= generated <= now + dt.timedelta(minutes=5)
                and edition.get('test_mode') is False
                and all(isinstance(edition.get(k), list) and len(edition[k]) == 15
                        and all(isinstance(story, dict) and story.get('title')
                                and isinstance(story.get('url'), str)
                                and story['url'].startswith('https://www.channelnewsasia.com/')
                                for story in edition[k])
                        and len({story['url'] for story in edition[k]}) == 15
                        for k in ('singapore', 'world')))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def decide(edition, runs, now):
    target = latest_window(now)
    title = 'News recovery ' + target.isoformat()
    common = {'target': target.isoformat(), 'recovery_title': title}
    if now - target < GRACE:
        return {**common, 'action': 'skip', 'reason': 'Allowing the main job 20 minutes to start'}
    if edition_fresh(edition, target, now):
        return {**common, 'action': 'skip', 'reason': 'This news edition is already complete'}
    if any(r.get('status') in ACTIVE for r in runs):
        return {**common, 'action': 'skip', 'reason': 'A news refresh is already queued or running'}
    attempts = [r for r in runs if r.get('event') == 'workflow_dispatch'
                and r.get('display_title') == title]
    if len(attempts) >= MAX_RECOVERIES:
        return {**common, 'action': 'skip', 'reason': 'Three wake-up recovery requests already made; scheduled backups remain enabled'}
    if attempts and now - max(timestamp(r['created_at']) for r in attempts) < COOLDOWN:
        return {**common, 'action': 'skip', 'reason': 'Waiting 20 minutes between recovery requests'}
    return {**common, 'action': 'dispatch', 'reason': 'The expected complete news edition is missing'}


class GitHub:
    def __init__(self, repo, token):
        self.base = 'https://api.github.com/repos/' + repo
        self.headers = {'Accept': 'application/vnd.github+json',
                        'Authorization': 'Bearer ' + token,
                        'X-GitHub-Api-Version': '2022-11-28'}

    def request(self, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=payload,
                                     headers=self.headers, method='GET' if body is None else 'POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            text = response.read()
        return json.loads(text) if text else {}

    def edition(self):
        try:
            data = self.request('/contents/data/daily-news.json?ref=main')
            return json.loads(base64.b64decode(data['content']))
        except (ValueError, KeyError, TypeError):
            return {}
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return {}
            raise


def recover(client, now, dry_run=False):
    edition = client.edition()
    runs = client.request('/actions/workflows/' + WORKFLOW + '/runs?branch=main&per_page=100')['workflow_runs']
    decision = decide(edition, runs, now)
    if decision['action'] == 'dispatch':
        # Avoid a redundant launch if another job just published the edition.
        if edition_fresh(client.edition(), latest_window(now), now):
            return {**decision, 'action': 'skip', 'reason': 'Another job just completed this edition'}
        if dry_run:
            return {**decision, 'action': 'would_dispatch'}
        client.request('/actions/workflows/' + WORKFLOW + '/dispatches',
                       {'ref': 'main', 'inputs': {'recoveryWindow': decision['target']}})
    return decision


if __name__ == '__main__':
    client = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
    result = recover(client, dt.datetime.now(dt.timezone.utc),
                     dry_run=os.environ.get('DRY_RUN') == 'true')
    print(json.dumps(result, indent=2))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as output:
            output.write('Daily News wake-up: ' + result['action'] + '\n\n'
                         + result['reason'] + '\n\nEdition due: ' + result['target'] + '\n')
