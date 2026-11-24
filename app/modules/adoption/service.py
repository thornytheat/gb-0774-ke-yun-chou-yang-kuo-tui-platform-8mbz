"""在签发页面接用补正签发。

补正签发是同一批扩推的新一版已签发结果，原排班沿用上一版（基准）签发。
接用时：

- 已发车分层保留原排班人数，不接用补正人数；
- 未发车且未被改层牵连的分层照常接用补正人数；
- 一端落在已发车分层的改层整笔留待人工，牵累沿改层关系冻结相连分层，
  避免改层只换一边而多算或漏算客流；
- 两端都未发车的改层与两层人数组成一个调整组，在同一事务内一起生效；
- 每接用一组即记入台账，重试时已接用的组不再重复生效。

排班只认已签发（原件与结果摘要校验通过、人口对平）的扩推结果。
"""

from app.errors import SurveyProblem
from app.modules.release.audit import authenticate

from .plan import departed_layers, plan, profile, schedule_profile
from .store import empty_document


def _signed(archive, key):
    manifest, tables, results = archive.read(key)
    authenticate(manifest, tables, results)
    return manifest, tables, results


def _baseline(store, archive, base_key, schedule_id):
    """取得原签发对应的排班文档。未建立时给出初始文档（不落库）。"""
    schedule_id = schedule_id or base_key
    _, _, results = _signed(archive, base_key)
    if store.exists(schedule_id):
        state = store.read(schedule_id)
        if state['document']['origin'] != base_key:
            raise SurveyProblem('SCHEDULE_MISMATCH', '排班基准与所指原签发不一致', 409)
        return schedule_id, state['document'], False
    return schedule_id, empty_document(base_key, profile(results), ()), True


def _departed(document, requested):
    """台账已记的已发车分层 + 本次指定，去重并校验存在。"""
    layers = set(document['current']['layers'])
    strangers = sorted(set(requested) - layers)
    if strangers:
        raise SurveyProblem('NO_LAYER', f'原签发中没有分层：{"、".join(strangers)}', 404)
    known = departed_layers(document['current'])
    return known + [name for name in requested if name not in known]


def _scheme(document, results, departed):
    """以排班现况对补正排队。

    比的是当前排班与补正，重试时已生效的组自然不再出现在待接用清单里；
    失败组因事务回滚仍保留旧人数，会再次出现在清单中，可由调用方重试。
    """
    return plan(schedule_profile(document['current']), profile(results), departed)


def preview(archive, store, base_key, corrected_key, departed=(), schedule_id=None):
    """只读方案：不改动排班，供签发页在接用前复核（本次 departed 不写入台账）。"""
    schedule_id, document, _ = _baseline(store, archive, base_key, schedule_id)
    _, _, results = _signed(archive, corrected_key)
    scheme = _scheme(document, results, _departed(document, departed))
    scheme.update(schedule=schedule_id, base=base_key, corrected=corrected_key,
                  current=document['current'])
    return scheme


def adopt(archive, store, base_key, corrected_key, departed=(), schedule_id=None):
    """按组接用补正签发。

    可接用的调整逐组落库，每组一个事务：组内改层两端与两层人数一起生效，
    或一起回滚。中途失败时已接用的组保留，失败组两端都保持旧排班人数，
    可整批重试；重试时已接用的组由台账跳过，不重复生效。
    """
    schedule_id, document, fresh = _baseline(store, archive, base_key, schedule_id)
    _, _, results = _signed(archive, corrected_key)  # 补正未过校验则不落任何排班
    document = store.create(schedule_id, document) if fresh else document
    if departed:
        document = store.mark_departed(schedule_id, departed)
    after = profile(results)
    scheme = _scheme(document, results, departed_layers(document['current']))

    applied, already = [], []
    for component in scheme['components']:
        try:
            fresh_group = store.adopt(schedule_id, corrected_key, component, after)
        except SurveyProblem:
            raise
        except Exception as exc:  # 组内事务已回滚，两端保持旧人数
            raise SurveyProblem(
                'ADOPT_FAILED',
                f'接用调整组 {component["item"]} 失败，已接用 {len(applied)} 组，可重试', 500) from exc
        (applied if fresh_group else already).append(component['item'])

    done = [row['item'] for row in store.history(schedule_id)
            if row['run'] == corrected_key and row['item'] not in applied]
    scheme.update(schedule=schedule_id, base=base_key, corrected=corrected_key,
                  applied=applied, already=list(dict.fromkeys(already + done)),
                  current=store.read(schedule_id)['document']['current'])
    return scheme


def mark_departed(store, schedule_id, layers, archive=None):
    """登记已发车分层。排班尚未建立时按原签发初始化，方便先登记后接用。"""
    if not layers:
        raise SurveyProblem('BAD_INPUT', '请指明已发车的分层')
    if archive is not None and not store.exists(schedule_id):
        _, _, results = _signed(archive, schedule_id)
        store.create(schedule_id, empty_document(schedule_id, profile(results), ()))
    store.mark_departed(schedule_id, layers)
    return view(store, schedule_id)


def view(store, schedule_id):
    state = store.read(schedule_id)
    return dict(id=state['id'], created=state['created'], updated=state['updated'],
                document=state['document'], history=store.history(schedule_id))


def reset(store, schedule_id):
    store.clear(schedule_id)
    return dict(id=schedule_id, cleared=True)
