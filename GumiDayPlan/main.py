"""GumiDayPlan web application.

In VS Code, click Run Python File on this file (main.py).
On the first run, a project-local .venv is created and requirements installed.
The browser opens at http://localhost:8000/.
"""
from __future__ import annotations


def _bootstrap_when_run_directly() -> None:
    """Use a project-local environment so the Anaconda base environment stays intact."""
    import hashlib
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent
    if sys.version_info < (3, 10):
        raise SystemExit('Python 3.10 이상이 필요합니다. 다른 Python 인터프리터를 선택해 주세요.')

    python = root / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    # When already running inside this project's virtual environment, continue
    # to import and run the web app rather than spawning another Python process.
    if os.path.normcase(str(Path(sys.executable).resolve())) == os.path.normcase(str(python.resolve())):
        return

    requirements = root / 'requirements.txt'
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    ready = root / '.venv' / '.gumi_requirements_installed'
    try:
        if not python.is_file():
            print('[구미에서 뭐하지?] 프로젝트 전용 Python 환경을 생성합니다...', flush=True)
            subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')], check=True, cwd=root)
        if not ready.is_file() or ready.read_text(encoding='utf-8') != fingerprint:
            print('[구미에서 뭐하지?] 필요한 라이브러리를 설치합니다 (최초 1회).', flush=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(requirements)],
                           check=True, cwd=root)
            ready.write_text(fingerprint, encoding='utf-8')
        print('[구미에서 뭐하지?] 웹 프로그램을 시작합니다.', flush=True)
        completed = subprocess.run([str(python), str(root / 'main.py')], cwd=root)
        raise SystemExit(completed.returncode)
    except subprocess.CalledProcessError as exc:
        print(f'설치/실행 오류 (종료코드 {exc.returncode}). 인터넷 연결과 Python 버전을 확인하세요.',
              file=sys.stderr)
        raise SystemExit(exc.returncode) from exc


if __name__ == '__main__':
    _bootstrap_when_run_directly()
import csv, io, json
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from src.config import ROOT, Settings
from src.models import Criteria,SearchRequest,SyncRequest,GeocodeRequest
from src.catalog import import_excellent_csv,demo_places,deduplicate
from src.db import Store
from src.providers import TourAPI,GumiRestaurantsAPI,NaverMaps,NaverLocal,ProviderError
from src.recommender import score_places
from src.planner import plan

settings=Settings()
store=Store(settings.db_path)

@asynccontextmanager
async def lifespan(app):
    import_excellent_csv(store)
    yield

app=FastAPI(title='구미에서 뭐하지?',version='1.1.0',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=str(ROOT/'static')),name='static')


@app.get('/')
def index(): return FileResponse(ROOT/'static'/'index.html')


@app.get('/api/status')
def status():
    public=store.list_places()
    return {'app':'구미에서 뭐하지?','version':'1.1.0',
        'keys':{'tourapi':settings.has_public,'naver_maps':settings.has_maps,
                'naver_local':settings.has_search,'gemini':settings.has_ai},
        # Maps Client ID is public by design in the official JS SDK; never expose secret.
        'maps_client_id':settings.naver_maps_id if settings.has_maps else '',
        'gemini_model':settings.gemini_model,
        'places':len(public),'with_coordinates':sum(p.has_location for p in public),
        'sources':store.counts(),
        'notes':['API 키는 서버 환경변수에서만 읽습니다.','NAVER 검색 결과는 AI에 전송하거나 DB에 저장하지 않습니다.']}


@app.get('/api/places')
def places(category:str='',with_coords:bool=False,limit:int=500):
    pp=deduplicate(store.list_places())
    if category:pp=[p for p in pp if p.category==category]
    if with_coords:pp=[p for p in pp if p.has_location]
    return {'items':[p.json() for p in pp[:min(max(1,limit),800)]],
            'total':len(pp),'located':sum(p.has_location for p in pp)}


@app.post('/api/sync/tour')
async def sync_tour(req:SyncRequest):
    def work():
        pts,stats=TourAPI(settings).list_places(max_pages=req.max_pages,max_results=req.limit)
        saved=store.upsert(pts)
        return {'ok':True,'saved':saved,**stats,'message':'관광공사 TourAPI 구미시 주소 확인 후 저장 완료'}
    try:return await run_in_threadpool(work)
    except ProviderError as e:raise HTTPException(502,str(e))


@app.post('/api/sync/restaurants')
async def sync_restaurants(req:SyncRequest):
    def work():
        pts,stats=GumiRestaurantsAPI(settings).list_places(limit=req.limit)
        return {'ok':True,'saved':store.upsert(pts),**stats,
                'message':'구미시 일반음식점 주소/업종을 추가했습니다. 위치 좌표는 별도 변환 필요.'}
    try:return await run_in_threadpool(work)
    except ProviderError as e:raise HTTPException(502,str(e))


@app.post('/api/geocode')
async def geocode(req:GeocodeRequest):
    if not settings.has_maps:raise HTTPException(400,'NAVER Maps 키를 먼저 .env에 설정하세요.')
    def work():
        client=NaverMaps(settings)
        targets=[p for p in deduplicate(store.list_places()) if not p.has_location and p.address
                 and (req.source_filter=='all' or p.source==req.source_filter)]
        targets=targets[:req.max_calls]
        success=0;failures=[];cache_hits=0
        for p in targets:
            cached=store.cached_geocode(p.address)
            if cached:cache_hits+=1;coords=cached
            else:
                try: coords=client.geocode(p.address)
                except ProviderError as e:failures.append(p.name+': '+str(e));continue
                if coords:store.put_geocode(p.address,*coords)
            if coords:store.update_place_location(p.id,*coords);success+=1
            else:failures.append(p.name+': 주소 좌표를 찾지 못함')
        return {'ok':True,'checked':len(targets),'located':success,'cached':cache_hits,
                'errors':failures[:12],'remaining':max(0,len([p for p in store.list_places() if not p.has_location])-success)}
    return await run_in_threadpool(work)


@app.post('/api/naver-search')
async def search(req:SearchRequest):
    try:
        records=await run_in_threadpool(lambda:NaverLocal(settings).search(req.query))
        return {'source':'NAVER API HUB 지역 검색','standalone':True,'items':records,
                'note':'검색 결과 원본·순서 유지. 추천 AI/자체 DB에 전송 또는 저장하지 않습니다. 네이버 장소 링크로 이동 가능.'}
    except ProviderError as e:raise HTTPException(502,str(e))


@app.post('/api/plan')
async def make_plan(criteria:Criteria):
    # Itinerary is explicitly a same-day schedule, never an overnight trip.
    start_hour,start_minute=map(int,criteria.start_time.split(':'))
    if start_hour*60+start_minute+criteria.hours*60>24*60:
        raise HTTPException(422,'당일치기 일정이 자정을 넘어갑니다. 출발시간을 앞당기거나 총 이용 시간을 줄여 주세요.')
    if criteria.demo:
        pool=demo_places()
    else:
        pool=deduplicate(store.list_places())
        pool=[p for p in pool if p.has_location]
        if not pool:
            raise HTTPException(409,'실제 공공데이터 장소의 좌표가 아직 없습니다. 관광 API 연동/주소 좌표변환을 먼저 해주세요. 가상 시연 모드에서는 즉시 테스트할 수 있어요.')
    # AI handles only public-sector or explicitly synthetic example records.
    # Limit potential token/cost and guarantee deterministic fallback.
    located=sorted(pool,key=lambda p:(p.lat-criteria.origin_lat)**2+(p.lon-criteria.origin_lon)**2)[:70]
    scores,score_mode=await run_in_threadpool(lambda:score_places(located,criteria,settings))
    from src.providers import NaverMaps
    maps=NaverMaps(settings) if criteria.precise and settings.has_maps else None
    try:
        result=await run_in_threadpool(lambda:plan(located,scores,criteria,maps,route_call_cap=settings.route_max_calls))
    except ValueError as e:raise HTTPException(422,str(e))
    if 'error' in result:raise HTTPException(409,result['error'])
    if criteria.use_ai and not score_mode.startswith('Gemini 취향 평가'):
        result.setdefault('warnings',[]).insert(0, 'AI 모델 평가가 적용되지 않았습니다. '+score_mode+' (추천 점수는 공공데이터 키워드 기반입니다.)')
    result.update({'recommendation_source':score_mode,
                   'place_pool':len(located),'demo':criteria.demo,
                   'input':criteria.model_dump()})
    return result


@app.post('/api/export/csv')
def export_csv(doc:dict=Body(...)):
    itinerary=doc.get('itinerary') or doc
    if not isinstance(itinerary,dict):raise HTTPException(422,'코스 데이터가 올바르지 않습니다.')
    stops=itinerary.get('stops') or []
    buf=io.StringIO(); writer=csv.writer(buf)
    writer.writerow(['순서','장소명','유형','도착시간','출발시간','이동시간(분)','체류시간(분)','주소',
                     '취향적합도','배경 데이터','비용(확정만)','운영시간 확인여부'])
    def safe_cell(v):
        text=str(v) if v is not None else ''
        return "'"+text if text.startswith(('=', '+', '-', '@')) else text
    for i,p in enumerate(stops,1):
        writer.writerow([i,safe_cell(p.get('name','')),safe_cell(p.get('category','')),p.get('arrival',''),
                         p.get('departure',''),p.get('travel_min',''),p.get('stay_min',''),
                         safe_cell(p.get('address','')),p.get('fit_score',''),safe_cell(p.get('source','')),
                         p.get('price_krw','') if p.get('price_verified') else '미확인',
                         '확인됨' if p.get('hours_verified') else '미확인'])
    body='\ufeff'+buf.getvalue()
    return StreamingResponse(iter([body.encode('utf-8')]),media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename="GumiDayPlan_itinerary.csv"'})

@app.get('/api/health')
def health(): return {'ok':True}


if __name__ == '__main__':
    import threading
    import webbrowser
    import uvicorn

    url = 'http://localhost:8000/'
    print(f'\n[구미에서 뭐하지?] 브라우저 주소: {url}', flush=True)
    print('[종료 방법] 실행 중인 VS Code 터미널에서 Ctrl+C', flush=True)
    timer = threading.Timer(2.0, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()
    uvicorn.run(app, host='127.0.0.1', port=8000, reload=False, log_level='info')
