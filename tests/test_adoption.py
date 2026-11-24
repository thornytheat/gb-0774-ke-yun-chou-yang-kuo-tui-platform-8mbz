import sqlite3

import pytest

from app.errors import SurveyProblem
from app.modules.adoption import service as adoption
from app.modules.adoption.plan import plan, profile
from app.modules.adoption.store import ScheduleStore
from app.modules.ledger.identity import canonical
from app.modules.ledger.service import archive
from app.modules.ledger.storage import Archive
from app.pipeline import run

BASE_FRAME = [('T1', 1, '甲', 100), ('T2', 1, '乙', 60), ('T7', 1, '乙', 40),
              ('T3', 1, '丙', 60), ('T6', 1, '丙', 40), ('T4', 1, '丁', 100),
              ('T5', 1, '戊', 100)]
BASE_ANSWERS = [
    'a1,T1,甲,1,YES,1/2,direct', 'a2,T1,甲,1,YES,1/2,transfer',
    'b1,T2,乙,1,YES,1/2,direct', 'b2,T7,乙,1,YES,1/2,transfer',
    'c1,T3,丙,1,YES,1/2,direct', 'c2,T6,丙,1,YES,1/2,transfer',
    'd1,T4,丁,1,YES,1/2,direct', 'd2,T4,丁,1,YES,1/2,transfer',
    'e1,T5,戊,1,YES,1/2,direct', 'e2,T5,戊,1,YES,1/2,transfer',
]


def build(frame, answers):
    frame_csv = 'trip,revision,layer,tickets\n' + ''.join(
        f'{trip},{revision},{layer},{tickets}\n' for trip, revision, layer, tickets in frame)
    returns_csv = 'ticket,trip,reported_layer,visit,status,probability,choice\n' + '\n'.join(answers) + '\n'
    return {'frame': frame_csv, 'returns': returns_csv}


@pytest.fixture
def base():
    return build(BASE_FRAME, BASE_ANSWERS)


@pytest.fixture
def corrected(base):
    """补正签发：乙的 0002 班次改到甲，丙的 0003 班次改到丁，戊的问项份额变化。"""
    frame = BASE_FRAME + [('T2', 2, '甲', 60), ('T3', 2, '丁', 60)]
    answers = BASE_ANSWERS[:-1] + ['e2,T5,戊,1,YES,1/2,other']
    return build(frame, answers)


@pytest.fixture
def stores(tmp_path, base, corrected):
    path = tmp_path / 'adoption.db'
    runs = Archive(path)
    schedules = ScheduleStore(path)
    base_key = archive(runs, base)['id']
    corrected_key = archive(runs, corrected)['id']
    return runs, schedules, base_key, corrected_key


def _ints(current, name):
    return current['layers'][name]['integers']


def test_plan_keeps_departed_and_adopts_rest(stores):
    runs, _, base_key, corrected_key = stores
    before = run(build(BASE_FRAME, BASE_ANSWERS))
    after = run(build(BASE_FRAME + [('T2', 2, '甲', 60), ('T3', 2, '丁', 60)],
                       BASE_ANSWERS[:-1] + ['e2,T5,戊,1,YES,1/2,other']))
    scheme = plan(profile(before), profile(after), ['甲'])
    assert (scheme['layers']['甲']['action'], scheme['layers']['甲']['reason']) == ('keep', 'DEPARTED')
    assert scheme['layers']['乙']['action'] == 'keep' and scheme['layers']['乙']['reason'] == 'IMPLICATED'
    assert scheme['layers']['丙']['action'] == 'adopt'
    assert scheme['layers']['丁']['action'] == 'adopt'
    assert scheme['layers']['戊']['action'] == 'adopt'
    assert [move['action'] for move in scheme['moves'] if move['trip'] == 'T2'] == ['defer']
    assert [move['action'] for move in scheme['moves'] if move['trip'] == 'T3'] == ['apply']


def test_both_undeparted_move_in_one_component(stores):
    runs, _, base_key, corrected_key = stores
    before = run(build(BASE_FRAME, BASE_ANSWERS))
    after = run(build(BASE_FRAME + [('T3', 2, '丁', 60)],
                       BASE_ANSWERS[:-1] + ['e2,T5,戊,1,YES,1/2,other']))
    scheme = plan(profile(before), profile(after), ['甲'])
    group = next(item for item in scheme['components'] if '丙' in item['layers'])
    assert group['layers'] == ['丁', '丙'] and [move['trip'] for move in group['moves']] == ['T3']


def test_adopt_persists_and_is_idempotent(stores):
    runs, schedules, base_key, corrected_key = stores
    first = adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert first['applied'] and not first['already']
    document = first['current']
    # 已发车甲保留旧人数；乙被牵连也保留旧人数
    assert _ints(document, '甲') == {'direct': 50, 'transfer': 50, 'other': 0}
    assert _ints(document, '乙') == {'direct': 50, 'transfer': 50, 'other': 0}
    # 丙、丁、戊接用补正
    assert _ints(document, '丁') == {'direct': 107, 'transfer': 53, 'other': 0}
    assert _ints(document, '戊') == {'direct': 50, 'transfer': 0, 'other': 50}
    assert document['trips']['T3'] == {'layer': '丁', 'tickets': 60}
    assert document['trips']['T2'] == {'layer': '乙', 'tickets': 60}   # 改层留待人工

    second = adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert second['already'] == first['applied'] and not second['applied']
    assert second['current'] == document


def test_preview_is_read_only(stores):
    runs, schedules, base_key, corrected_key = stores
    adoption.preview(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert not schedules.exists(base_key)


def test_unrelated_layer_adopts_while_departed_kept(stores):
    runs, schedules, base_key, corrected_key = stores
    scheme = adoption.adopt(runs, schedules, base_key, corrected_key, departed=['戊'])
    document = scheme['current']
    assert _ints(document, '戊') == {'direct': 50, 'transfer': 50, 'other': 0}
    assert _ints(document, '甲') == run(build(
        BASE_FRAME + [('T2', 2, '甲', 60), ('T3', 2, '丁', 60)],
        BASE_ANSWERS[:-1] + ['e2,T5,戊,1,YES,1/2,other']))['allocation']['layers']['甲']['integers']
    assert document['trips']['T2'] == {'layer': '甲', 'tickets': 60}
    assert document['trips']['T3'] == {'layer': '丁', 'tickets': 60}


def test_failed_group_rolls_back_both_sides_then_retry(stores):
    runs, schedules, base_key, corrected_key = stores

    real_adopt = schedules.adopt
    calls = {'n': 0}

    def flaky(schedule_id, run_key, component, after):
        calls['n'] += 1
        if calls['n'] == 2 and '戊' in component['layers']:
            raise sqlite3.OperationalError('database is locked')
        return real_adopt(schedule_id, run_key, component, after)

    schedules.adopt = flaky
    with pytest.raises(SurveyProblem) as error:
        adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert error.value.code == 'ADOPT_FAILED'

    # 已接用的组（丁、丙）保留，失败组（戊）还是旧人数
    partial = schedules.read(base_key)['document']['current']
    assert _ints(partial, '丁') == {'direct': 107, 'transfer': 53, 'other': 0}
    assert _ints(partial, '丙') == {'direct': 0, 'transfer': 40, 'other': 0}
    assert partial['trips']['T3'] == {'layer': '丁', 'tickets': 60}
    assert _ints(partial, '戊') == {'direct': 50, 'transfer': 50, 'other': 0}

    schedules.adopt = real_adopt
    retried = adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert retried['applied'] == ['component:戊']
    assert _ints(retried['current'], '戊') == {'direct': 50, 'transfer': 0, 'other': 50}
    # 已接用的丁、丙没有被重复处理
    history = schedules.history(base_key)
    assert len(history) == len({row['item'] for row in history}) == 2


def test_failed_move_component_keeps_both_layers(tmp_path):
    """改层两端的组在落库中途失败时，两层人数与班次层都保留旧值。"""
    path = tmp_path / 'abort.db'
    runs, schedules = Archive(path), ScheduleStore(path)
    base_key = archive(runs, build(BASE_FRAME, BASE_ANSWERS))['id']
    corrected_key = archive(runs, build(
        BASE_FRAME + [('T3', 2, '丁', 60)], BASE_ANSWERS[:-1] + ['e2,T5,戊,1,YES,1/2,other']))['id']

    def abort(schedule_id, run_key, component, after):
        if '丁' in component['layers']:
            raise sqlite3.OperationalError('aborted mid-group')
        return ScheduleStore.adopt(schedules, schedule_id, run_key, component, after)

    schedules.adopt = abort
    with pytest.raises(SurveyProblem):
        adoption.adopt(runs, schedules, base_key, corrected_key)
    current = schedules.read(base_key)['document']['current']
    # 改层两端丙、丁都保持旧人数，班次 T3 仍是丙
    assert _ints(current, '丙') == {'direct': 50, 'transfer': 50, 'other': 0}
    assert _ints(current, '丁') == {'direct': 50, 'transfer': 50, 'other': 0}
    assert current['trips']['T3'] == {'layer': '丙', 'tickets': 60}
    assert schedules.history(base_key) == []



def test_registered_departed_layer_survives_other_corrections(stores):
    runs, schedules, base_key, corrected_key = stores
    adoption.mark_departed(schedules, base_key, ['戊'], runs)
    scheme = adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    current = scheme['current']
    assert current['layers']['戊']['integers'] == {'direct': 50, 'transfer': 50, 'other': 0}
    # 戊已在台账登记，后续补正也保留；甲本次指定，同批也不动
    assert current['layers']['甲']['integers'] == {'direct': 50, 'transfer': 50, 'other': 0}
    assert current['layers']['戊']['departed'] is True
    # 本次经接用登记的甲也写入台账，后续补正继续保留
    assert current['layers']['甲']['departed'] is True


def test_rejects_unknown_departed_layer(stores):
    runs, schedules, base_key, corrected_key = stores
    with pytest.raises(SurveyProblem) as error:
        adoption.adopt(runs, schedules, base_key, corrected_key, departed=['不存在'])
    assert error.value.code == 'NO_LAYER'


def test_tampered_correction_is_not_adopted(stores):
    runs, schedules, base_key, corrected_key = stores
    _, _, results = runs.read(corrected_key)
    results['balance']['balanced'] = False
    with runs.connect() as db:
        db.execute('UPDATE runs SET results=? WHERE id=?',
                   (canonical(results), corrected_key))
    with pytest.raises(SurveyProblem) as error:
        adoption.adopt(runs, schedules, base_key, corrected_key, departed=['甲'])
    assert error.value.code in ('BAD_SEAL', 'UNBALANCED')
    assert not schedules.exists(base_key)


def test_api_preview_and_adopt(client):
    base_key = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    corrected = {'tables': {
        'returns': open('sample_data/dirty/returns.csv').read(),
        'frame': open('sample_data/dirty/frame.csv').read() + '0003,2,枢纽,75\n',
    }}
    corrected_key = client.post('/api/runs', json=corrected).json['id']
    preview = client.get('/api/adoption', query_string={
        'base': base_key, 'corrected': corrected_key, 'departed': '末班'})
    assert preview.status_code == 200
    assert preview.json['layers']['末班']['action'] == 'keep'
    assert preview.json['layers']['城北']['action'] == 'adopt'

    answer = client.post('/api/adoption', json={
        'base': base_key, 'corrected': corrected_key, 'departed': ['末班']})
    assert answer.status_code == 200 and answer.json['applied']
    document = answer.json['current']
    assert document['layers']['末班']['integers'] == dict.fromkeys(('direct', 'transfer', 'other'), 0)
    assert document['layers']['城北']['integers'] == {'direct': 0, 'transfer': 0, 'other': 25}
    assert document['trips']['0003'] == {'layer': '枢纽', 'tickets': 75}

    again = client.post('/api/adoption', json={
        'base': base_key, 'corrected': corrected_key, 'departed': ['末班']})
    assert again.json['already'] == answer.json['applied']


def test_new_correction_readjusts_from_current_schedule(client):
    """同一原签发下换一份补正，按排班现况重新比较，可把改层挪回去。"""
    base_key = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    frame, returns = (open(f'sample_data/dirty/{name}.csv').read() for name in ('frame', 'returns'))
    moved = client.post('/api/runs', json={'tables': {
        'returns': returns, 'frame': frame + '0003,2,枢纽,75\n'}}).json['id']
    settled = client.post('/api/runs', json={'tables': {
        'returns': returns + '\n00099,0002,枢纽,1,YES,1/2,direct\n', 'frame': frame}}).json['id']

    first = client.post('/api/adoption', json={'base': base_key, 'corrected': moved, 'departed': ['末班']}).json
    assert first['current']['trips']['0003'] == {'layer': '枢纽', 'tickets': 75}

    second = client.post('/api/adoption', json={'base': base_key, 'corrected': settled, 'departed': ['末班']}).json
    current = second['current']
    assert current['trips']['0003'] == {'layer': '城北', 'tickets': 75}
    assert current['layers']['城北']['integers'] == {'direct': 34, 'transfer': 33, 'other': 33}
    assert current['layers']['末班']['integers'] == dict.fromkeys(('direct', 'transfer', 'other'), 0)

