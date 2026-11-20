"""③ 자동 운영 판단(5분 주기): 빈 방 운전·이상 후보 → Jev 판단 → 건물 스위치에 따라 자동 조치 또는 제안

- 후보(빈 방 운전, kind=idle): 지금·15분 전·30분 전 모두 재실 0명으로 운전(src/recommend.py 'idle' 규칙과 같은 기준, src/sim.py로 계산)
- 후보(이상, kind=anomaly): 지금 설정온도 미도달(sim.state anomaly). Jev 없이 알림(status=alert)
- Jev 판단(빈 방): room_empty = empty / occupied / uncertain + 확신도. 키·호출 실패 시 판단 없음으로 제안(폴백)
- 결정 게이트(빈 방)
  - Jev가 occupied를 기준 이상 확신 → skipped(조치·제안 없음, 쿨다운 동안 다시 묻지 않음)
  - 스위치 ON + Jev가 empty를 기준(AUTO_APPLY_CONFIDENCE, 기본 0.9) 이상 확신 → 끄기 스케줄 저장, applied(되돌리기 가능)
  - 그 외 → pending 제안(SUGGESTION_TTL_MIN, 기본 30분 뒤 만료)
- 중복 방지: 진행 중(pending·applied·accepted) 호실, 쿨다운(COOLDOWN_MIN, 기본 120분) 안의 거절·되돌림·재실 판단·알림 호실은 건너뜀
- Jev 호출 상한: 실행당 MAX_JEV_PER_RUN(기본 10)건, 넘는 후보는 다음 실행으로 미룸(deferred)
- 만료: 응답 기한이 지난 pending을 expired로 바꿈
- 로컬 확인(Lambda 폴더에서, DynamoDB·Jev 호출 없음): python -X utf8 infra/ops/functions/auto_operator.py --dry-run
"""
import datetime as dt
import json
import logging
import os
import time
import urllib.request

import ops_common as oc

log = logging.getLogger()
log.setLevel(logging.INFO)

JEV_URL = 'https://api.typesafe.ai/v1/systemone'
IDLE_MINUTES = (0, 15, 30)
QUESTIONS = {
    'room_empty': {
        'type': 'choice',
        'instructions': ('건물 운영 디지털트윈의 호실 상태 기록을 보고, 지금 이 호실이 실제로 비어 있어 냉난방을 꺼도 되는지 판단하라. '
                         'recent는 0·15·30분 전 상태다. 운영 시간·요일·용도(회의실은 예약 사이 공백, 진료실은 환자 대기 등)를 고려하라.'),
        'criteria': {
            'empty': '30분 동안 재실이 없었고 곧 사용할 정황도 없어 꺼도 됨',
            'occupied': '센서상 0명이어도 곧 사용하거나 사용 중일 가능성이 높아 끄면 안 됨',
            'uncertain': '근거가 부족해 사람이 확인해야 함',
        },
    },
}

_key = {'value': None}


def at_minute(event=None):
    import sim
    raw = (event or {}).get('time')
    t = dt.datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(sim.KST) if raw else sim.now()
    return t.replace(second=0, microsecond=0)


def is_idle(room, t, schedules):
    import sim
    return all(s['onOff'] and s['occupants'] == 0
               for s in (sim.state(room, t - dt.timedelta(minutes=m), schedules) for m in IDLE_MINUTES))


def jev_state(b, room, t, schedules):
    """Jev에 넘기는 근거(사실만, 판단 문구 없음)"""
    import sim
    recent = []
    for m in IDLE_MINUTES:
        s = sim.state(room, t - dt.timedelta(minutes=m), schedules)
        recent.append({'minutesAgo': m, 'occupants': s['occupants'], 'onOff': s['onOff'], 'powerKw': s['powerKw'],
                       'temp': s['temp'], 'setpoint': s['setpoint']})
    h0, h1 = room.get('hours', (9, 18))
    return {
        'building': {'name': b['name'], 'use': b['use'], 'operatingHours': f'{h0:02d}~{h1:02d}시', 'weekendOperation': bool(b.get('weekend'))},
        'now': t.isoformat(), 'weekday': '월화수목금토일'[t.weekday()],
        'room': {'name': room['name'], 'use': room['use'], 'floor': room['floor'], 'areaM2': room['areaM2']},
        'recent': recent,
    }


def _api_key():
    if _key['value'] is None:
        import boto3
        raw = boto3.client('secretsmanager').get_secret_value(SecretId=os.environ['JEV_SECRET_ID'])['SecretString'].strip()
        if raw.startswith('{'):
            d = json.loads(raw)
            raw = d.get('apiKey') or d.get('key') or ''
        _key['value'] = raw.strip()
    return _key['value']


def ask_jev(state):
    """{'choice', 'confidence'}. 실패는 예외(호출 측에서 폴백)"""
    body = {'model': os.environ.get('JEV_MODEL', 'jev-latest'), 'state': state, 'questions': QUESTIONS}
    req = urllib.request.Request(os.environ.get('JEV_URL', JEV_URL), json.dumps(body, ensure_ascii=False).encode(),
                                 {'Authorization': f'Bearer {_api_key()}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        a = json.loads(r.read(1_000_000)).get('answers', {}).get('room_empty') or {}
    if a.get('choice') not in QUESTIONS['room_empty']['criteria']:
        raise ValueError('Jev 응답에 room_empty 선택 없음')
    return {'choice': a['choice'], 'confidence': float(a.get('confidence') or 0)}


def judge(state, ask=None):
    """Jev 판단 또는 None(키 없음·호출 실패·응답 이상). 키·응답 원문은 로그에 남기지 않음"""
    try:
        return (ask or ask_jev)(state)
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({'jev': 'failed', 'reason': type(e).__name__}))
        return None


def gate(answer, auto_apply, threshold):
    """빈 방 후보의 결정: 'skipped' · 'applied' · 'pending'"""
    if answer and answer['choice'] == 'occupied' and answer['confidence'] >= threshold:
        return 'skipped'
    if auto_apply and answer and answer['choice'] == 'empty' and answer['confidence'] >= threshold:
        return 'applied'
    return 'pending'


def run(t, schedules, ask=None, write=True):
    """전 건물 1회 판단. write=False면 저장 없이 결정 목록만 반환(로컬 dry-run)"""
    import buildings
    import sim
    threshold = oc.env_float('AUTO_APPLY_CONFIDENCE', 0.9)
    ttl_min = oc.env_int('SUGGESTION_TTL_MIN', 30)
    cooldown = oc.env_int('COOLDOWN_MIN', 120)
    max_jev = oc.env_int('MAX_JEV_PER_RUN', 10)
    since = t - dt.timedelta(minutes=max(cooldown, ttl_min) + 5)
    ttl = int((t + dt.timedelta(days=oc.HISTORY_DAYS)).timestamp())
    out, counts, jev_calls = [], {'pending': 0, 'applied': 0, 'skipped': 0, 'alert': 0, 'expired': 0, 'deferred': 0}, 0
    buildings.forget()
    for b in buildings.LIST:
        bid = b['id']
        settings = oc.get_settings(bid) if write else {'autoApply': False}
        recent = oc.recent_decisions(bid, since) if write else []
        for d in recent:
            if oc.is_expired(d, t) and oc.update_status(bid, d['decisionId'], 'expired', t):
                d['status'] = 'expired'
                counts['expired'] += 1
        blocked = oc.blocked_rooms(recent, t, cooldown)
        for room in buildings.rooms(bid):
            if room.get('active') is False:
                continue
            rid = room['id']
            base = {'buildingId': bid, 'roomId': rid, 'roomName': room['name'], 'floor': room['floor'], 'use': room['use'],
                    'createdAt': t.isoformat(), 'ttl': ttl}
            st = sim.state(room, t, schedules)
            if st['anomaly'] and ('anomaly', rid) not in blocked:
                out.append({**base, 'decisionId': oc.new_decision_id(t), 'kind': 'anomaly', 'status': 'alert',
                            'message': oc.anomaly_message(room, st),
                            'evidence': {k: st[k] for k in ('temp', 'setpoint', 'mode', 'onOff', 'anomaly')}})
                counts['alert'] += 1
            if ('idle', rid) in blocked or not is_idle(room, t, schedules):
                continue
            if jev_calls >= max_jev:
                counts['deferred'] += 1
                continue
            jev_calls += 1
            state = jev_state(b, room, t, schedules)
            answer = judge(state, ask)
            status = gate(answer, settings['autoApply'], threshold)
            minutes = IDLE_MINUTES[-1]
            item = {**base, 'decisionId': oc.new_decision_id(t), 'kind': 'idle', 'status': status, 'jev': answer,
                    'autoApply': settings['autoApply'], 'evidence': {'recent': state['recent']}}
            if status == 'applied':
                sched = oc.off_schedule(bid, rid, t, f"{room['floor']} {room['name']} {minutes}분 재실 0명")
                if write:
                    oc.save_schedule(sched)
                item.update(scheduleId=sched['id'], message=oc.applied_message(room, minutes), decidedAt=t.isoformat(), decidedBy=oc.SOURCE)
            elif status == 'pending':
                item.update(message=oc.idle_message(room, minutes, st['powerKw']),
                            expiresAt=(t + dt.timedelta(minutes=ttl_min)).isoformat())
            else:
                item.update(message=None, decidedAt=t.isoformat(), decidedBy='Jev')
            out.append(item)
            counts[status] += 1
    if write:
        for item in out:
            oc.put_decision({k: v for k, v in item.items() if v is not None})
    return out, counts, jev_calls


def handler(event, context):
    import common
    t0 = time.monotonic()
    t = at_minute(event)
    _, counts, calls = run(t, common.list_schedules())
    summary = {'at': t.isoformat(), **counts, 'jevCalls': calls, 'ms': round((time.monotonic() - t0) * 1000)}
    log.info(json.dumps(summary))
    return summary


if __name__ == '__main__':
    import argparse
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / 'src'))
    a = argparse.ArgumentParser(description='auto-operator 로컬 실행(저장·Jev 호출 없음)')
    a.add_argument('--dry-run', action='store_true', required=True)
    a.add_argument('--at', help='기준 시각 ISO(KST), 기본 현재')
    args = a.parse_args()
    if not sys.flags.utf8_mode:
        sys.exit('python -X utf8 로 실행하십시오(팀 모듈의 JSON 읽기가 시스템 인코딩을 따름)')
    import sim
    t = (dt.datetime.fromisoformat(args.at).replace(tzinfo=sim.KST) if args.at else sim.now()).replace(second=0, microsecond=0)
    os.environ.setdefault('MAX_JEV_PER_RUN', '1000')
    t1 = time.monotonic()
    out, counts, calls = run(t, [], ask=lambda s: {'choice': 'uncertain', 'confidence': 0.5}, write=False)
    print(f'기준 {t.isoformat()} 결정 {len(out)}건 {counts} 판단 대상 {calls} 계산 {round((time.monotonic() - t1) * 1000)}ms')
    for item in out[:5]:
        print(' ', item['kind'], item['status'], item.get('message'))
