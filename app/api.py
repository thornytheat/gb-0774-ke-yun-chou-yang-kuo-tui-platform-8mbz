from flask import Blueprint,current_app,jsonify,request

from app.errors import SurveyProblem
from app.modules.adoption import service as adoption
from app.modules.ledger.service import archive
from app.modules.release.service import release
from app.pipeline import STAGES,run
from app.source import sources

survey = Blueprint('survey',__name__,url_prefix='/api')


def storage():
    return current_app.extensions['survey_archive']


def schedules():
    return current_app.extensions['survey_schedule']


def _body():
    body=request.get_json(silent=True)
    if not isinstance(body,dict):
        raise SurveyProblem('BAD_INPUT','请提交 JSON 参数')
    return body


def _layers(value, name='layers'):
    if value is None:
        return []
    if isinstance(value,str):
        return [value] if value else []
    if isinstance(value,list) and all(isinstance(item,str) and item for item in value):
        return list(dict.fromkeys(value))
    raise SurveyProblem('BAD_INPUT',f'{name} 应为分层名或分层名列表')


def _pick(*values):
    for value in values:
        if value:
            return value
    raise SurveyProblem('BAD_INPUT','请指明原签发与补正签发的归档编号')


@survey.route('/survey/<stage>',methods=['GET','POST'])
def inspect(stage):
    if stage not in STAGES:
        raise SurveyProblem('NO_STAGE','找不到该统计阶段',404)
    if request.method=='GET' and 'run' in request.args:
        result=storage().read(request.args['run'])[2]
    else:
        body=request.get_json(silent=True) if request.method=='POST' else {'sample':request.args.get('sample','dirty')}
        result=run(sources(body),stage)
    return jsonify(result[stage])


@survey.get('/runs')
def list_runs():
    return jsonify(runs=storage().recent())


@survey.post('/runs')
def new_run():
    out=archive(storage(),sources(request.get_json(silent=True)))
    return jsonify(out), 200 if out['reused'] else 201


@survey.get('/runs/<key>')
def get_run(key):
    manifest,_,results=storage().read(key)
    return jsonify(id=key,manifest=manifest,balance=results['balance'],allocation=results['allocation'])


@survey.post('/runs/<key>/rerun')
def rerun(key):
    manifest,tables,_=storage().read(key)
    return jsonify(archive(storage(),tables,manifest))


@survey.get('/export/<key>')
def export(key):
    response=jsonify(release(*storage().read(key)))
    response.headers['Content-Disposition']=f'attachment; filename="survey-{key[:12]}.json"'
    return response


@survey.get('/adoption')
def adoption_preview():
    args=request.args
    scheme=adoption.preview(storage(),schedules(),
                            _pick(args.get('base')),_pick(args.get('corrected')),
                            _layers(args.getlist('departed')),args.get('schedule'))
    return jsonify(scheme)


@survey.post('/adoption')
def adoption_apply():
    body=_body()
    scheme=adoption.adopt(storage(),schedules(),
                          _pick(body.get('base')),_pick(body.get('corrected')),
                          _layers(body.get('departed')),body.get('schedule'))
    return jsonify(scheme)


@survey.get('/schedule/<key>')
def schedule_state(key):
    return jsonify(adoption.view(schedules(),key))


@survey.post('/schedule/<key>/departed')
def schedule_departed(key):
    return jsonify(adoption.mark_departed(schedules(),key,_layers(_body().get('layers')),storage()))


@survey.delete('/schedule/<key>')
def schedule_clear(key):
    return jsonify(adoption.reset(schedules(),key))
