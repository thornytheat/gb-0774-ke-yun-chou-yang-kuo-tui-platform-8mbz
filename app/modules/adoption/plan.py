"""接用补正签发的纯计算。

比较「原排班」与「补正扩推」的差异，排出可接用与留待人工的调整：

- 已发车分层保留原排班人数，不接用补正人数；
- 未发车且与已发车分层无改层牵连的分层，照常接用新人数；
- 出票改层只要一端落在已发车分层，整笔留待人工；牵累沿改层关系扩散到
  相连的未发车分层，避免只换一边而多算或漏算客流；
- 两端都未发车的改层与两层的排班人数组成同一调整组，由调用方在同一
  事务内一起生效，失败则两端都保留旧人数。

本模块不做持久化，输入输出都是纯字典，便于单独复核。
"""

from collections import defaultdict

from app.config import CHOICES


def zero_layer():
    return dict(integers=dict.fromkeys(CHOICES, 0), unknown=0, tickets=0)


def _layer(integers, unknown, tickets):
    return dict(integers={key: int(integers[key]) for key in CHOICES},
                unknown=int(unknown), tickets=int(tickets))


def profile(results):
    """从一次扩推结果中取出排班所需的最小画像。"""
    population = results['frame']['population']
    layers = {name: _layer(row['integers'], row['unobserved'], population.get(name, 0))
              for name, row in results['allocation']['layers'].items()}
    trips = {trip: dict(layer=history[-1]['layer'], tickets=int(history[-1]['tickets']))
             for trip, history in results['frame']['trips'].items()}
    return dict(layers=layers, trips=trips)


def schedule_profile(document):
    """把排班存档还原成与 profile() 同形的画像（忽略发车标记）。"""
    return dict(layers={name: _layer(value['integers'], value.get('unknown', 0),
                                     value.get('tickets', 0))
                        for name, value in document['layers'].items()},
                trips={trip: dict(layer=value['layer'], tickets=int(value['tickets']))
                       for trip, value in document['trips'].items()})


def departed_layers(document):
    return sorted(name for name, value in document['layers'].items() if value.get('departed'))


def component_key(layers):
    return 'component:' + ','.join(sorted(layers))


def _endpoints(move):
    return {name for name in (move['from_layer'], move['to_layer']) if name is not None}


def _moves(before, after):
    moves = []
    for trip in sorted(set(before['trips']) | set(after['trips'])):
        old, new = before['trips'].get(trip), after['trips'].get(trip)
        if old and new and old['layer'] == new['layer'] and old['tickets'] == new['tickets']:
            continue
        if old and new:
            kind = 'move' if old['layer'] != new['layer'] else 'tickets'
        else:
            kind = 'remove' if old else 'add'
        moves.append(dict(trip=trip, kind=kind,
                          from_layer=old['layer'] if old else None,
                          to_layer=new['layer'] if new else None,
                          before_tickets=old['tickets'] if old else None,
                          after_tickets=new['tickets'] if new else None))
    return moves


def _numbers_differ(before, after):
    return (before is None or after is None
            or before['integers'] != after['integers']
            or before['unknown'] != after['unknown']
            or before['tickets'] != after['tickets'])


def _components(nodes, moves):
    parent = {name: name for name in nodes}

    def find(name):
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            parent[right] = left

    for move in moves:
        ends = sorted(_endpoints(move))
        for other in ends[1:]:
            union(ends[0], other)

    members = defaultdict(set)
    for name in nodes:
        members[find(name)].add(name)

    result = []
    for group in members.values():
        attached = [move for move in moves if _endpoints(move) <= group]
        result.append(dict(layers=sorted(group), moves=attached,
                           item=component_key(group)))
    return sorted(result, key=lambda row: row['item'])


def plan(before, after, departed):
    """排出接用方案。

    before/after 与 profile() 同形；departed 为已发车分层集合。
    补正中已不存在的分层按零人数参与比较（该层已无出票）。
    返回的 components 即为需要原子落库的调整组。
    """
    departed = set(departed)
    names = sorted(set(before['layers']) | set(after['layers']))
    effective = {name: after['layers'].get(name, zero_layer()) for name in names}
    moves = _moves(before, after)

    # 牵累从已发车分层沿改层关系扩散：相连的未发车分层也不能单独接用，
    # 否则改层只换一边会多算或漏算客流。
    adjacency = defaultdict(set)
    for move in moves:
        ends = sorted(_endpoints(move))
        for left in ends:
            for right in ends:
                if left != right:
                    adjacency[left].add(right)
                    adjacency[right].add(left)
    tainted = set(departed)
    pending = list(tainted)
    while pending:
        node = pending.pop()
        for neighbour in adjacency[node]:
            if neighbour not in tainted:
                tainted.add(neighbour)
                pending.append(neighbour)

    for move in moves:
        ends = _endpoints(move)
        if ends & departed:
            move.update(action='defer', reason='DEPARTED')
        elif ends & tainted:
            move.update(action='defer', reason='IMPLICATED')
        else:
            move.update(action='apply', reason=None)
    applied_moves = [move for move in moves if move['action'] == 'apply']

    adopt = {name for name in names
             if name not in tainted
             and _numbers_differ(before['layers'].get(name), effective[name])}
    for move in applied_moves:
        adopt |= _endpoints(move)

    layers = {}
    for name in names:
        if name in adopt:
            action, reason = 'adopt', None
        elif name in departed:
            action, reason = 'keep', 'DEPARTED'
        elif name in tainted:
            action, reason = 'keep', 'IMPLICATED'
        else:
            action, reason = 'unchanged', None
        layers[name] = dict(action=action, reason=reason,
                            before=before['layers'].get(name),
                            after=effective[name], present=name in after['layers'])

    return dict(layers=layers, moves=moves,
                components=_components(adopt, applied_moves),
                adopted=sorted(adopt), departed=sorted(departed),
                deferred=sorted(move['trip'] for move in moves if move['action'] == 'defer'),
                implicated=sorted(name for name, row in layers.items()
                                  if row['action'] == 'keep' and row['reason'] == 'IMPLICATED'))


def apply_component(document, component, after):
    """把一个调整组写入排班现况（调用方保证事务）。"""
    for name in component['layers']:
        state = after['layers'].get(name) or zero_layer()
        document['layers'][name] = dict(integers=dict(state['integers']),
                                        unknown=state['unknown'], tickets=state['tickets'],
                                        departed=bool(document['layers'].get(name, {}).get('departed')))
    for move in component['moves']:
        trip = move['trip']
        if move['kind'] == 'remove':
            document['trips'].pop(trip, None)
        else:
            document['trips'][trip] = dict(layer=move['to_layer'], tickets=move['after_tickets'])


def satisfied(document, component, after):
    """排班现况是否已具备该调整组的目标值（用于识别重复接用）。"""
    for name in component['layers']:
        target = after['layers'].get(name) or zero_layer()
        row = document['layers'].get(name)
        if not row or row['integers'] != target['integers'] or row['unknown'] != target['unknown'] \
                or row['tickets'] != target['tickets']:
            return False
    for move in component['moves']:
        row = document['trips'].get(move['trip'])
        if move['kind'] == 'remove':
            if row is not None:
                return False
        elif not row or row != dict(layer=move['to_layer'], tickets=move['after_tickets']):
            return False
    return True
