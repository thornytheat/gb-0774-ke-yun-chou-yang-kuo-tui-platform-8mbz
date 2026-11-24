from fractions import Fraction

from flask import Blueprint, render_template, request

from app.api import schedules, storage
from app.config import CHOICES, SPEC
from app.errors import SurveyProblem
from app.modules.adoption import service as adoption
from app.modules.release.service import release
from app.pipeline import run
from app.reasons import REASONS
from app.source import sources

workbench = Blueprint('workbench', __name__)
TABS = {'balance':'层级偏差对账','returns':'抽样回收采信','frame':'班次与分层校验','weights':'非响应权重校准',
        'allocation':'扩推整数分配','archive':'批次留档复跑','release':'样本证据签发'}
REASONS_TEXT = {**REASONS, 'DEPARTED':'分层已发车', 'IMPLICATED':'牵连已发车改层'}


def decimal_text(value, suffix=''):
    return '—' if value is None else f'{float(Fraction(value)):.2f}{suffix}'


@workbench.get('/')
@workbench.get('/survey/<tab>')
def desk(tab='balance'):
    if tab not in TABS:
        raise SurveyProblem('NO_STAGE','找不到业务页',404)
    sample = request.args.get('sample','dirty')
    key = request.args.get('run')
    manifest = None
    if key:
        manifest, tables, result = storage().read(key)
    else:
        tables = sources({'sample':sample})
        result = run(tables)
    layer = request.args.get('layer') or next(iter(result['frame']['population']))
    if layer not in result['frame']['population']:
        raise SurveyProblem('NO_LAYER','找不到该分层',404)
    proof = release(manifest,tables,result) if manifest and tab=='release' else None
    corrected = request.args.get('corrected') if tab=='release' else None
    adoption_view, schedule, departed = None, None, []
    if key and schedules().exists(key):
        schedule = adoption.view(schedules(),key)
        departed = sorted(name for name,row in schedule['document']['current']['layers'].items()
                          if row.get('departed'))
    if key and corrected:
        departed = list(dict.fromkeys(departed + request.args.getlist('departed')))
        try:
            adoption_view = adoption.preview(storage(),schedules(),key,corrected,departed)
        except SurveyProblem as problem:
            adoption_view = dict(error=problem.code,message=str(problem))
    shifts = [float(abs(Fraction(row['shift_pp']))) for row in result['balance']['bias'].values() if row['shift_pp'] is not None]
    scale = max([5, *(5 * (int(value / 5) + 1) for value in shifts)])
    return render_template('base.html',tab=tab,tabs=TABS,sample=sample,key=key,manifest=manifest,data=result,layer=layer,
                           spec=SPEC,choices=CHOICES,reasons={**REASONS,**REASONS_TEXT},decimal=decimal_text,maximum=scale,
                           fraction=Fraction, proof=proof, recent=storage().recent() if tab in ('archive','release') else [],
                           corrected=corrected, adoption=adoption_view, schedule=schedule, departed=departed)

