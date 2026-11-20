"""③ 자동 운영 오프라인 테스트(DynamoDB·Jev 없이, 파일 출처 건물·호실, 메모리 저장소)

실행(Lambda 폴더에서): python -X utf8 -m unittest discover -s infra/ops/tests
"""
import datetime as dt
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'infra' / 'ops' / 'functions'))
for k in ('BUILDINGS_TABLE', 'ROOMS_TABLE', 'OWNERS_TABLE', 'SCHEDULES_TABLE', 'MEMBERS_TABLE', 'COMPANIES_TABLE'):
    os.environ.pop(k, None)

import auto_operator as op  # noqa: E402
import buildings  # noqa: E402
import ops_api  # noqa: E402
import ops_common as oc  # noqa: E402
import sim  # noqa: E402
import tenant  # noqa: E402

T = dt.datetime(2026, 11, 20, 19, 0, tzinfo=sim.KST)  # 운영 시간 뒤: 빈 방 운전 후보가 생기는 시각
EMPTY = lambda s: {'choice': 'empty', 'confidence': 0.95}  # noqa: E731


class Store:
    """ops_common 저장소 함수를 메모리로 대체"""

    def __init__(self, auto=False):
        self.settings = {}
        self.decisions = {}
        self.schedules = {}
        self.auto = auto

    def patch(self):
        def update_status(bid, did, status, t, by=None, extra=None, expect='pending'):
            d = self.decisions.get((bid, did))
            if not d or d['status'] != expect:
                return False
            d.update(status=status, decidedAt=t.isoformat(), **({'decidedBy': by} if by else {}), **(extra or {}))
            return True
        return mock.patch.multiple(
            oc,
            get_settings=lambda bid: {'buildingId': bid, 'autoApply': self.settings.get(bid, self.auto), 'updatedAt': None, 'updatedBy': None},
            put_settings=lambda bid, v, by, t: self.settings.__setitem__(bid, v) or {'buildingId': bid, 'autoApply': v},
            recent_decisions=lambda bid, since: sorted((dict(d) for (b, _), d in self.decisions.items() if b == bid), key=lambda d: d['decisionId']),
            get_decision=lambda bid, did: dict(self.decisions[(bid, did)]) if (bid, did) in self.decisions else None,
            put_decision=lambda item: self.decisions.__setitem__((item['buildingId'], item['decisionId']), dict(item)),
            update_status=update_status,
            save_schedule=lambda s: self.schedules.__setitem__(s['id'], s),
            delete_schedule=lambda sid: self.schedules.pop(sid, None) is not None,
        )

    def by_status(self, status, kind='idle'):
        return [d for d in self.decisions.values() if d['status'] == status and d['kind'] == kind]


class GateTest(unittest.TestCase):
    def test_gate(self):
        self.assertEqual(op.gate({'choice': 'empty', 'confidence': 0.95}, True, 0.9), 'applied')
        self.assertEqual(op.gate({'choice': 'empty', 'confidence': 0.95}, False, 0.9), 'pending')  # 스위치 OFF는 항상 제안
        self.assertEqual(op.gate({'choice': 'empty', 'confidence': 0.7}, True, 0.9), 'pending')   # 확신도 낮으면 제안
        self.assertEqual(op.gate({'choice': 'occupied', 'confidence': 0.95}, True, 0.9), 'skipped')
        self.assertEqual(op.gate({'choice': 'uncertain', 'confidence': 0.99}, True, 0.9), 'pending')
        self.assertEqual(op.gate(None, True, 0.9), 'pending')  # Jev 실패 폴백

    def test_judge_failure_returns_none(self):
        def boom(state):
            raise TimeoutError()
        self.assertIsNone(op.judge({}, boom))

    def test_idle_matches_recommend_rule(self):
        import recommend
        for room in buildings.rooms('main'):
            hit = recommend._rule(room, T, [])
            self.assertEqual(op.is_idle(room, T, []), bool(hit and hit[0] == 'idle'), room['id'])


class BlockTest(unittest.TestCase):
    def d(self, status, minutes_ago, **kw):
        t = T - dt.timedelta(minutes=minutes_ago)
        return {'kind': 'idle', 'roomId': 'r', 'status': status, 'createdAt': t.isoformat(), 'decidedAt': t.isoformat(), **kw}

    def test_blocked_rooms(self):
        exp = lambda m: (T + dt.timedelta(minutes=m)).isoformat()  # noqa: E731
        self.assertIn(('idle', 'r'), oc.blocked_rooms([self.d('pending', 5, expiresAt=exp(25))], T, 120))
        self.assertNotIn(('idle', 'r'), oc.blocked_rooms([self.d('pending', 40, expiresAt=exp(-10))], T, 120))  # 만료
        self.assertIn(('idle', 'r'), oc.blocked_rooms([self.d('rejected', 100)], T, 120))
        self.assertNotIn(('idle', 'r'), oc.blocked_rooms([self.d('rejected', 130)], T, 120))  # 쿨다운 끝
        self.assertIn(('idle', 'r'), oc.blocked_rooms([self.d('applied', 300)], T, 120))  # 자동 조치는 되돌리기 전까지 유지
        self.assertNotIn(('idle', 'r'), oc.blocked_rooms([self.d('expired', 10)], T, 120))


class OperatorTest(unittest.TestCase):
    def run_once(self, store, t=T, ask=EMPTY, env=None):
        with store.patch(), mock.patch.dict(os.environ, env or {}):
            return op.run(t, [], ask=ask)

    def test_switch_off_makes_pending_suggestions(self):
        s = Store(auto=False)
        out, counts, calls = self.run_once(s)
        self.assertGreater(counts['pending'], 0)
        self.assertEqual(counts['applied'], 0)
        self.assertEqual(s.schedules, {})
        p = s.by_status('pending')[0]
        self.assertIn('끌까요?', p['message'])
        self.assertEqual(p['expiresAt'], (T + dt.timedelta(minutes=30)).isoformat())

    def test_switch_on_applies_with_schedule(self):
        s = Store(auto=True)
        _, counts, _ = self.run_once(s)
        self.assertGreater(counts['applied'], 0)
        a = s.by_status('applied')[0]
        sched = s.schedules[a['scheduleId']]
        self.assertEqual((sched['action'], sched['condition'], sched['roomIds'], sched['time']), ('off', 'unused', [a['roomId']], '19:00'))
        self.assertTrue(sched['sourceText'].startswith('자동 운영'))
        self.assertIn('껐어요', a['message'])

    def test_jev_cap_defers(self):
        s = Store()
        _, counts, calls = self.run_once(s, env={'MAX_JEV_PER_RUN': '2'})
        self.assertEqual(calls, 2)
        self.assertEqual(counts['pending'], 2)
        self.assertGreater(counts['deferred'], 0)

    def test_no_duplicate_next_run(self):
        s = Store()
        first, _, _ = self.run_once(s)
        second, counts, _ = self.run_once(s, t=T + dt.timedelta(minutes=5))
        rooms = lambda out, kind: {d['roomId'] for d in out if d['kind'] == kind}  # noqa: E731
        # 응답 대기 중인 호실은 다시 묻지 않음(두 번째 실행은 상한으로 미뤄진 호실만)
        self.assertFalse(rooms(first, 'idle') & rooms(second, 'idle'))
        self.assertEqual(counts['alert'], 0)  # 알림도 쿨다운

    def test_expired_then_resuggested(self):
        s = Store()
        self.run_once(s)
        _, counts, _ = self.run_once(s, t=T + dt.timedelta(minutes=35))
        self.assertGreater(counts['expired'], 0)
        self.assertGreater(len(s.by_status('expired')), 0)

    def test_occupied_skipped(self):
        s = Store(auto=True)
        _, counts, _ = self.run_once(s, ask=lambda st: {'choice': 'occupied', 'confidence': 0.95})
        self.assertGreater(counts['skipped'], 0)
        self.assertEqual(counts['applied'] + counts['pending'], 0)

    def test_jev_state_has_no_judgement(self):
        room = buildings.rooms('main')[0]
        st = op.jev_state(buildings.get('main'), room, T, [])
        self.assertEqual([r['minutesAgo'] for r in st['recent']], [0, 15, 30])
        self.assertNotIn('rule', st)


def event(method, bid='main', body=None, **path):
    return {'requestContext': {'http': {'method': method}, 'authorizer': {'jwt': {'claims': {'sub': 'u1'}}}},
            'pathParameters': {'id': bid, **path}, 'body': json.dumps(body) if body is not None else None}


class ApiTest(unittest.TestCase):
    def call(self, store, ev, role='operator'):
        with store.patch(), mock.patch.object(tenant, 'caller', lambda e: {'sub': 'u1', 'role': role, 'companyId': 'c1', 'email': None}), \
                mock.patch.object(ops_api, '_now', lambda: T + dt.timedelta(minutes=1)):
            r = ops_api.handler(ev, None)
        return r['statusCode'], json.loads(r['body'])

    def seeded(self, auto=False):
        s = Store(auto=auto)
        with s.patch():
            op.run(T, [], ask=EMPTY)
        return s

    def test_overview(self):
        s = self.seeded()
        code, body = self.call(s, event('GET'))
        self.assertEqual(code, 200)
        self.assertFalse(body['settings']['autoApply'])
        self.assertEqual(body['pendingCount'], len(s.by_status('pending')))
        self.assertNotIn('ttl', body['decisions'][0])

    def test_switch_roles(self):
        s = Store()
        self.assertEqual(self.call(s, event('PUT', body={'autoApply': True}), role='operator')[0], 403)
        self.assertEqual(self.call(s, event('PUT', body={'autoApply': True}), role='manager')[0], 200)
        self.assertEqual(self.call(s, event('PUT', body={'autoApply': True}), role='admin')[0], 200)
        self.assertEqual(self.call(s, event('PUT', body={'autoApply': 'yes'}), role='manager')[0], 400)
        self.assertTrue(s.settings['main'])

    def test_accept_saves_schedule_once(self):
        s = self.seeded()
        did = s.by_status('pending')[0]['decisionId']
        code, body = self.call(s, event('POST', decisionId=did, action='accept'))
        self.assertEqual(code, 200)
        self.assertEqual(body['status'], 'accepted')
        self.assertIn(body['scheduleId'], s.schedules)
        self.assertEqual(self.call(s, event('POST', decisionId=did, action='accept'))[0], 409)  # 두 번째 수락

    def test_reject(self):
        s = self.seeded()
        did = s.by_status('pending')[0]['decisionId']
        self.assertEqual(self.call(s, event('POST', decisionId=did, action='reject'))[1]['status'], 'rejected')
        self.assertEqual(s.schedules, {})

    def test_undo_applied(self):
        s = self.seeded(auto=True)
        a = s.by_status('applied')[0]
        code, body = self.call(s, event('POST', decisionId=a['decisionId'], action='undo'))
        self.assertEqual((code, body['status']), (200, 'reverted'))
        self.assertNotIn(a['scheduleId'], s.schedules)
        self.assertEqual(self.call(s, event('POST', decisionId=a['decisionId'], action='undo'))[0], 409)

    def test_expired_cannot_accept(self):
        s = self.seeded()
        did = s.by_status('pending')[0]['decisionId']
        with mock.patch.object(ops_api, '_now', lambda: T + dt.timedelta(minutes=31)):
            with s.patch(), mock.patch.object(tenant, 'caller', lambda e: {'sub': 'u1', 'role': 'operator', 'companyId': 'c1', 'email': None}):
                r = ops_api.handler(event('POST', decisionId=did, action='accept'), None)
        self.assertEqual(r['statusCode'], 409)

    def test_bad_action_and_unknown(self):
        s = self.seeded()
        self.assertEqual(self.call(s, event('POST', decisionId='x', action='delete'))[0], 400)
        self.assertEqual(self.call(s, event('POST', decisionId='nope', action='accept'))[0], 404)
        self.assertEqual(self.call(s, event('GET', bid='no-such'))[0], 404)

    def test_forbidden_building(self):
        s = Store()
        with s.patch(), mock.patch.object(tenant, 'require_building', mock.Mock(side_effect=tenant.Forbidden())):
            r = ops_api.handler(event('GET'), None)
        self.assertEqual(r['statusCode'], 403)


if __name__ == '__main__':
    unittest.main()
