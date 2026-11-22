from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from src.config.settings import cache, engine
from src.controllers.workflow import download
from src.models.tables import audit, cases, evidence, runs
from src.schemas.workflow import BandwidthForm, CaseForm, LoginForm, ReviewForm, WindowForm
from src.services import booking, domain, workflow
from src.services.security import current_user, login, operator, reviewer

router = APIRouter()


@router.get('/health')
def health():
    with engine.connect() as conn:
        conn.execute(text('SELECT 1')).scalar_one()
    cache.ping()
    return {'ok': True}


@router.post('/login')
def sign_in(form: LoginForm):
    return login(form.username, form.password)


@router.get('/spec')
def spec(_=Depends(current_user)):
    return domain.SPEC


@router.get('/cases')
def list_cases(_=Depends(current_user)):
    return workflow.recent(cases)


@router.post('/cases', status_code=201)
def create_case(form: CaseForm, actor=Depends(operator)):
    return workflow.make_case(form.title, form.payload, actor)


@router.get('/runs')
def list_runs(_=Depends(current_user)):
    return workflow.recent(runs)


@router.post('/cases/{case_id}/runs', status_code=202)
def start_run(case_id: int, actor=Depends(operator)):
    return workflow.enqueue(case_id, actor)


@router.get('/evidence')
def list_evidence(_=Depends(current_user)):
    return workflow.recent(evidence)


@router.get('/evidence/{identity}/download')
def export_evidence(identity: int, _=Depends(current_user)):
    return download(identity)


@router.post('/evidence/{identity}/review')
def review_evidence(identity: int, form: ReviewForm, actor=Depends(reviewer)):
    return workflow.review(identity, form.note, actor)


@router.get('/audit')
def list_audit(_=Depends(current_user)):
    return workflow.recent(audit)


@router.get('/bandwidths')
def list_bandwidths(_=Depends(current_user)):
    return booking.list_bandwidths()


@router.put('/bandwidths')
def set_bandwidth(form: BandwidthForm, actor=Depends(reviewer)):
    return booking.set_bandwidth(form.node_id, form.bandwidth_bytes_per_hour, actor)


@router.get('/bookings')
def list_bookings(_=Depends(current_user)):
    return booking.recent()


@router.post('/evidence/{identity}/booking', status_code=201)
def create_booking(identity: int, form: WindowForm, response: Response, actor=Depends(operator)):
    row, existed = booking.reserve(identity, form.start_at, form.end_at, actor)
    if existed:
        response.status_code = 200
    return row


@router.post('/evidence/{identity}/booking/reschedule')
def reschedule_booking(identity: int, form: WindowForm, actor=Depends(operator)):
    return booking.reschedule(identity, form.start_at, form.end_at, actor)


@router.delete('/evidence/{identity}/booking')
def cancel_booking(identity: int, actor=Depends(operator)):
    return booking.cancel(identity, actor)
