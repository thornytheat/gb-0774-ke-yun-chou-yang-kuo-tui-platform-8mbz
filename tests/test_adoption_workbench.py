def test_release_page_has_adoption_panel(client):
    base_key = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    corrected = {'tables': {
        'returns': open('sample_data/dirty/returns.csv').read(),
        'frame': open('sample_data/dirty/frame.csv').read() + '0003,2,枢纽,75\n',
    }}
    corrected_key = client.post('/api/runs', json=corrected).json['id']
    page = client.get('/survey/release', query_string={
        'run': base_key, 'corrected': corrected_key, 'departed': '末班'})
    body = page.get_data(as_text=True)
    assert page.status_code == 200
    assert '接用补正签发' in body and '接用可生效调整' in body
    assert '城北' in body and '枢纽' in body


def test_departed_endpoint_and_view(client):
    base_key = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    # 先登记已发车分层（排班尚未建立时按原签发初始化）
    answer = client.post(f'/api/schedule/{base_key}/departed', json={'layers': ['末班']})
    assert answer.status_code == 200
    assert answer.json['document']['current']['layers']['末班']['departed'] is True
    state = client.get(f'/api/schedule/{base_key}')
    assert state.json['document']['current']['layers']['末班']['departed'] is True


def test_adopt_respects_registered_departure(client):
    base_key = client.post('/api/runs', json={'sample': 'dirty'}).json['id']
    frame, returns = (open(f'sample_data/dirty/{name}.csv').read() for name in ('frame', 'returns'))
    corrected = client.post('/api/runs', json={'tables': {
        'returns': returns + '\n00099,0002,枢纽,1,YES,1/2,direct\n', 'frame': frame}}).json['id']
    client.post(f'/api/schedule/{base_key}/departed', json={'layers': ['城北']})
    scheme = client.post('/api/adoption', json={'base': base_key, 'corrected': corrected}).json
    # 城北已登记发车，保留原排班人数
    assert scheme['current']['layers']['城北']['integers'] == {'direct': 34, 'transfer': 33, 'other': 33}
    assert '城北' in scheme['departed']


def test_store_rejects_missing_schedule(client):
    assert client.get('/api/schedule/missing').json['code'] == 'NO_SCHEDULE'
