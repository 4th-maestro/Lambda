"""도면 변환 작업 API 테스트: admin 전용, 작업 생성 검증, 업로드 키·서명, 시작·재시작 상태 전환, 인식 전용 작업, 상태 조회(가짜 저장소, 네트워크 없음)"""
import json
import os
import pathlib
import re
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))
# anthropic·boto3 미설치 환경에서도 실행되도록 모듈 대체(test_ws.py와 같은 방식)
sys.modules.setdefault('anthropic', types.SimpleNamespace(AnthropicBedrock=lambda **kw: None))
sys.modules.setdefault('boto3', types.SimpleNamespace(client=None))
import api  # noqa: E402
import plan_ingest  # noqa: E402
import tenant  # noqa: E402

ENV = {'PLANS_BUCKET': 'bk', 'PLAN_JOBS_TABLE': 'J', 'PLAN_STATE_MACHINE': 'arn:sm'}
ROOT = pathlib.Path(__file__).resolve().parent.parent


class Conditional(Exception):
    pass


class FakeJobs:
    """DynamoDB Table 대체: get·put, 조건 #s = :old(상태 일치)만 지원"""

    def __init__(self):
        self.items = {}
        self.meta = types.SimpleNamespace(client=types.SimpleNamespace(exceptions=types.SimpleNamespace(ConditionalCheckFailedException=Conditional)))

    def get_item(self, Key):
        it = self.items.get(Key['jobId'])
        return {'Item': json.loads(json.dumps(it))} if it else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None, **kw):
        if ConditionExpression and (self.items.get(Item['jobId']) or {}).get('status') != ExpressionAttributeValues[':old']:
            raise Conditional()
        self.items[Item['jobId']] = json.loads(json.dumps(Item))


class FakeS3:
    def __init__(self):
        self.signed, self.objects = [], {}

    def generate_presigned_url(self, method, Params, ExpiresIn):
        self.signed.append((method, Params, ExpiresIn))
        return f"https://{Params['Bucket']}.s3/{Params['Key']}?m={method}"

    def get_object(self, Bucket, Key):
        return {'Body': types.SimpleNamespace(read=lambda: self.objects[Key])}


class FakeSfn:
    def __init__(self, fail=False):
        self.started, self.fail = [], fail

    def start_execution(self, stateMachineArn, input):
        if self.fail:
            raise RuntimeError('throttled')
        self.started.append((stateMachineArn, json.loads(input)))


def http(route, body=None, path=None, groups='admin'):
    return {'routeKey': route, 'pathParameters': path, 'body': json.dumps(body) if body is not None else None,
            'requestContext': {'authorizer': {'jwt': {'claims': {'sub': 'u-admin', 'cognito:groups': groups}}}}}


def call(route, body=None, path=None, groups='admin'):
    r = api.handler(http(route, body, path, groups), None)
    return r['statusCode'], json.loads(r['body'])


PDF = {'building': 'main', 'kind': 'pdf', 'files': [{'name': '1층 평면도.pdf', 'size': 1234}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.jobs, self.s3, self.sfn = FakeJobs(), FakeS3(), FakeSfn()
        tenant._tables['J'] = self.jobs
        plan_ingest._c.update(s3=self.s3, stepfunctions=self.sfn)
        self.env = patch.dict(os.environ, ENV)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        tenant._tables.pop('J', None)
        plan_ingest._c.clear()

    def create(self, body=PDF):
        code, out = call('POST /plans/jobs', body)
        self.assertEqual(code, 201, out)
        return out


class AuthTest(Base):
    def test_admin_only(self):
        for route, body, path in (('POST /plans/jobs', PDF, None), ('POST /plans/jobs/{jobId}/start', {}, {'jobId': 'x'}),
                                  ('POST /plans/detect', {}, None), ('GET /plans/jobs/{jobId}', None, {'jobId': 'x'})):
            self.assertEqual(call(route, body, path, groups='')[0], 403, route)
        self.assertEqual(self.jobs.items, {})

    def test_routes(self):
        for p in ('/plans/jobs', '/plans/jobs/{jobId}/start', '/plans/detect', '/plans/jobs/{jobId}'):
            self.assertIs(api.route(p), plan_ingest.handler, p)


class CreateTest(Base):
    def test_validation(self):
        f = PDF['files']
        bad = [
            {**PDF, 'building': 'nope'}, {**PDF, 'building': 'b-ABCD1234'}, {**PDF, 'building': 'b-abc123'}, {**PDF, 'building': None},
            {**PDF, 'kind': 'dwg'}, {**PDF, 'kind': None},
            {**PDF, 'files': []}, {**PDF, 'files': 'a.pdf'}, {**PDF, 'files': f * 21},
            {**PDF, 'files': [{'name': 'a.dwg', 'size': 1}]},  # pdf 작업에 CAD 파일
            {**PDF, 'files': [{'name': 'a/b.pdf', 'size': 1}]}, {**PDF, 'files': [{'name': '..\\b.pdf', 'size': 1}]},
            {**PDF, 'files': [{'name': 'a.pdf', 'size': 200 * 1024 * 1024 + 1}]}, {**PDF, 'files': [{'name': 'a.pdf', 'size': 0}]},
            {**PDF, 'files': [{'name': 'a.pdf', 'size': True}]}, {**PDF, 'files': [{'name': 'a.pdf', 'size': 1.5}]},
            {**PDF, 'files': [{'name': 'a.pdf', 'size': 1}, {'name': 'a.pdf', 'size': 2}]},
            ['not', 'object'],
        ]
        for body in bad:
            self.assertEqual(call('POST /plans/jobs', body)[0], 400, body)
        self.assertEqual(self.jobs.items, {})
        self.assertEqual(self.s3.signed, [])

    def test_kinds_and_new_building(self):
        ok = [('ifc', ['a.IFC', 'equip.png']), ('cad', ['a.dxf', 'b.DWG']), ('pdf', ['a.pdf']), ('image', ['a.jpg', 'b.jpeg', 'c.png'])]
        for kind, names in ok:
            self.create({'building': 'b-abcd1234', 'kind': kind, 'files': [{'name': n, 'size': 10} for n in names]})
        self.assertEqual(len(self.jobs.items), 4)

    def test_upload_keys_and_signature(self):
        out = self.create({**PDF, 'files': [{'name': '1층 평면도.pdf', 'size': 1234}, {'name': '2층.pdf', 'size': 200 * 1024 * 1024}]})
        jid = out['jobId']
        self.assertEqual([u['key'] for u in out['uploads']], [f'uploads/{jid}/1층 평면도.pdf', f'uploads/{jid}/2층.pdf'])
        # PUT URL 15분, Content-Length 서명(신고 크기와 다른 업로드는 S3가 거부)
        self.assertEqual([(m, p['Key'], p['ContentLength'], e) for m, p, e in self.s3.signed],
                         [('put_object', f'uploads/{jid}/1층 평면도.pdf', 1234, 900), ('put_object', f'uploads/{jid}/2층.pdf', 200 * 1024 * 1024, 900)])
        job = self.jobs.items[jid]
        self.assertEqual((job['status'], job['building'], job['kind']), ('created', 'main', 'pdf'))
        self.assertAlmostEqual(job['ttl'], time.time() + 7 * 86400, delta=60)
        self.assertEqual(self.sfn.started, [])


class StartTest(Base):
    def start(self, jid, body=None):
        return call('POST /plans/jobs/{jobId}/start', body, {'jobId': jid})

    def test_start_and_conflict(self):
        jid = self.create()['jobId']
        self.assertEqual(self.start('nope')[0], 404)
        for bad in ({'options': {'dpi': 300}}, {'options': {'pxPerM': -1}}, {'options': {'scale': '1/200'}}, {'options': []}):
            self.assertEqual(self.start(jid, bad)[0], 400, bad)
        self.assertEqual(self.start(jid, {'options': {'scale': 200}}), (202, {'status': 'running'}))
        job = self.jobs.items[jid]
        self.assertEqual((job['status'], job['step']), ('running', 'parse'))
        arn, payload = self.sfn.started[0]
        self.assertEqual(arn, 'arn:sm')
        self.assertEqual(payload, {'jobId': jid, 'building': 'main', 'kind': 'pdf', 'keys': [f'uploads/{jid}/1층 평면도.pdf'],
                                   'bucket': 'bk', 'options': {'scale': 200}})
        self.assertEqual(self.start(jid)[0], 409)
        self.assertEqual(len(self.sfn.started), 1)

    def test_restart_after_failure(self):
        jid = self.create()['jobId']
        self.jobs.items[jid].update(status='failed', error='x')
        self.assertEqual(self.start(jid)[0], 202)
        self.assertNotIn('error', self.jobs.items[jid])
        self.assertEqual(self.sfn.started[0][1]['options'], {})
        self.jobs.items[jid]['status'] = 'review'
        self.assertEqual(self.start(jid)[0], 409)

    def test_concurrent_start(self):
        jid = self.create()['jobId']
        # 조회 뒤 다른 요청이 먼저 시작한 경우: 조건부 기록 실패 → 409, 상태 머신 시작 안 함
        real = self.jobs.get_item

        def read_then_race(Key):
            item = real(Key)
            self.jobs.items[jid]['status'] = 'running'
            return item
        self.jobs.get_item = read_then_race
        self.assertEqual(self.start(jid)[0], 409)
        self.assertEqual(self.sfn.started, [])

    def test_start_failure_marks_failed(self):
        jid = self.create()['jobId']
        plan_ingest._c['stepfunctions'] = FakeSfn(fail=True)
        with self.assertRaises(RuntimeError):
            self.start(jid)
        self.assertEqual(self.jobs.items[jid]['status'], 'failed')
        self.assertIn('throttled', self.jobs.items[jid]['error'])


class DetectTest(Base):
    def test_detect_only_job(self):
        src = self.create({'building': 'b-abcd1234', 'kind': 'image', 'files': [{'name': 'plan.png', 'size': 10}, {'name': 'plan2.png', 'size': 10}]})
        key = src['uploads'][0]['key']
        bad = [{'building': 'main', 'key': key},  # 다른 건물
               {'building': 'b-abcd1234', 'key': 'plans/main/current.json'}, {'building': 'b-abcd1234', 'key': f"uploads/{src['jobId']}/none.png"},
               {'building': 'b-abcd1234', 'key': key, 'pxPerM': 0}, {'building': 'b-abcd1234', 'key': key, 'pxPerM': True}, {'building': 'b-abcd1234'}]
        for body in bad:
            self.assertEqual(call('POST /plans/detect', body)[0], 400, body)
        code, out = call('POST /plans/detect', {'building': 'b-abcd1234', 'key': key, 'pxPerM': 40})
        self.assertEqual(code, 202)
        job = self.jobs.items[out['jobId']]
        self.assertEqual((job['kind'], job['status'], job['step'], job['files'][0]['key']), ('detect', 'running', 'detect', key))
        self.assertEqual(self.sfn.started[-1][1], {'jobId': out['jobId'], 'building': 'b-abcd1234', 'bucket': 'bk',
                                                   'detect': [{'floor': None, 'imageKey': key, 'pxPerM': 40, 'toPlan': None}]})
        # 인식 전용 작업은 /start로 다시 시작하지 않음
        job['status'] = 'failed'
        self.assertEqual(call('POST /plans/jobs/{jobId}/start', None, {'jobId': out['jobId']})[0], 409)

    def test_non_image_rejected(self):
        src = self.create()
        self.assertEqual(call('POST /plans/detect', {'building': 'main', 'key': src['uploads'][0]['key']})[0], 400)


class StatusTest(Base):
    def get(self, jid):
        return call('GET /plans/jobs/{jobId}', None, {'jobId': jid})

    def test_running_review_failed(self):
        jid = self.create()['jobId']
        self.assertEqual(self.get('nope')[0], 404)
        self.jobs.items[jid].update(status='running', step='detect')
        self.assertEqual(self.get(jid), (200, {'jobId': jid, 'building': 'main', 'kind': 'pdf', 'status': 'running', 'step': 'detect'}))
        # 상태 머신 SaveDraft 결과: 초안 URL, 층별 도면 이미지 URL과 크기
        self.jobs.items[jid].update(status='review', step='merge', draftKey=f'jobs/{jid}/draft.json')
        self.s3.objects[f'jobs/{jid}/draft.json'] = json.dumps({'schema': 'twin.building/v1', 'floors': [
            {'id': '1F', 'background': {'key': f'jobs/{jid}/pages/1.png', 'px': [3000, 2000], 'toPlan': None}},
            {'id': '2F'}]}).encode()
        code, out = self.get(jid)
        self.assertEqual((code, out['status']), (200, 'review'))
        self.assertEqual(out['draftUrl'], f'https://bk.s3/jobs/{jid}/draft.json?m=get_object')
        self.assertEqual(out['pages'], [{'floor': '1F', 'url': f'https://bk.s3/jobs/{jid}/pages/1.png?m=get_object', 'px': [3000, 2000]}])
        self.assertTrue(all(e == 900 for _, _, e in self.s3.signed))
        # 상태 머신 MarkFailed 결과: Lambda 오류는 errorMessage, 그 밖의 오류는 원문
        cause = json.dumps({'errorMessage': '도면 층을 찾지 못했습니다.', 'errorType': 'ValueError', 'stackTrace': []}, ensure_ascii=False)
        self.jobs.items[jid] = {**self.jobs.items[jid], 'status': 'failed', 'error': cause}
        del self.jobs.items[jid]['draftKey']
        self.assertEqual(self.get(jid)[1]['error'], '도면 층을 찾지 못했습니다.')
        self.jobs.items[jid]['error'] = 'States.Timeout'
        self.assertEqual(self.get(jid)[1]['error'], 'States.Timeout')

    def test_detect_job_has_no_pages(self):
        self.jobs.items['d1'] = {'jobId': 'd1', 'building': 'main', 'kind': 'detect', 'status': 'review', 'step': 'detect', 'draftKey': 'jobs/d1/draft.json'}
        out = self.get('d1')[1]
        self.assertEqual(out['draftUrl'], 'https://bk.s3/jobs/d1/draft.json?m=get_object')
        self.assertNotIn('pages', out)


class StateMachineTest(unittest.TestCase):
    """statemachine/plan.asl.json 구조: 전이 대상 존재, 오류 처리, 템플릿 치환 이름(식 평가는 README의 TestState 확인 기록)"""

    def test_definition(self):
        asl = json.loads((ROOT / 'statemachine/plan.asl.json').read_text())
        states = asl['States']
        inner = states['Detect']['ItemProcessor']['States']
        names = set(states) | set(inner)
        targets = [s.get('Next') or s.get('Default') for s in states.values()]
        targets += [c['Next'] for s in states.values() for c in s.get('Choices', []) + s.get('Catch', [])]
        self.assertTrue({t for t in targets if t} <= names)
        for name, s in states.items():
            if s['Type'] in ('Task', 'Map') and name != 'MarkFailed':
                self.assertEqual(s['Catch'][0]['Next'], 'MarkFailed', name)
        self.assertEqual(states['Detect']['MaxConcurrency'], 2)
        tpl = (ROOT / 'template.yaml').read_text()
        for sub in {m for m in re.findall(r'\$\{(\w+)\}', json.dumps(asl))}:
            self.assertIn(f'        {sub}: ', tpl, sub)


if __name__ == '__main__':
    unittest.main()
