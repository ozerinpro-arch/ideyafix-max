"""Only synthetic invoices: no banking, payments, taxes or external integrations."""
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict
from .auth import require_roles
from .db import edit_state

router = APIRouter(prefix='/api/finance')
SERVICES = [
    {'id': 'robotics', 'title': 'Робототехника', 'teacher': 'Волков А.И.', 'schedule': 'Вторник и четверг · 15:30', 'room': '309', 'tariffKopeks': 320000, 'color': 'blue'},
    {'id': 'chess', 'title': 'Шахматы', 'teacher': 'Петрова А.А.', 'schedule': 'Понедельник · 15:30', 'room': '207', 'tariffKopeks': 160000, 'color': 'purple'},
    {'id': 'english', 'title': 'Английский язык', 'teacher': 'Морозова С.Н.', 'schedule': 'Среда и пятница · 15:30', 'room': '208', 'tariffKopeks': 240000, 'color': 'green'},
]
ALLOWED_PERIOD = '2026-10'

def ensure_finance(state):
    if '_finance' not in state:
        state['_finance'] = {'invoices': [
            {'id': 'demo-001', 'studentId': 's1', 'studentName': 'Алексей Соколов', 'className': '7А', 'serviceId': 'chess', 'service': 'Шахматы', 'period': ALLOWED_PERIOD, 'amountKopeks': 160000, 'status': 'issued', 'date': '12.10.2026'},
            {'id': 'demo-002', 'studentId': 's2', 'studentName': 'Дарья Новикова', 'className': '7А', 'serviceId': 'english', 'service': 'Английский язык', 'period': ALLOWED_PERIOD, 'amountKopeks': 240000, 'status': 'draft', 'date': '12.10.2026'},
        ]}
    return state['_finance']

@router.get('')
def get_finance(profile=Depends(require_roles('director', 'accountant', 'student'))):
    with edit_state() as state:
        invoices = ensure_finance(state)['invoices']
        if profile['role'] == 'student':
            invoices = [i for i in invoices if i['studentId'] == profile['userId'] and i['status'] in ('issued', 'paid')]
        students = [{'id': s['id'], 'name': s['name'], 'className': s['className']} for s in state['students']] if profile['role'] in ('director','accountant') else []
        return {'services': SERVICES, 'invoices': invoices, 'students': students, 'demo': True, 'period': ALLOWED_PERIOD}

class CreateInvoice(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    studentId: Annotated[str, Field(min_length=1, max_length=50)]
    serviceId: Annotated[str, Field(min_length=1, max_length=30)]
    period: Annotated[str, Field(pattern=r'^2026-10$')]

@router.post('/invoices')
def create_invoice(body: CreateInvoice, profile=Depends(require_roles('director', 'accountant'))):
    service = next((s for s in SERVICES if s['id'] == body.serviceId), None)
    if not service:
        raise HTTPException(404, 'Кружок не найден')
    with edit_state() as state:
        student = next((s for s in state['students'] if s['id'] == body.studentId), None)
        if not student:
            raise HTTPException(404, 'Ученик не найден')
        data = ensure_finance(state)
        duplicate = next((i for i in data['invoices'] if i['studentId'] == body.studentId and i['serviceId'] == body.serviceId and i['period'] == body.period), None)
        if duplicate:
            return {'invoice': duplicate, 'duplicate': True}
        invoice = {'id': f'demo-{len(data["invoices"])+1:03d}', 'studentId': student['id'], 'studentName': student['name'], 'className': student['className'], 'serviceId': service['id'], 'service': service['title'], 'period': body.period, 'amountKopeks': service['tariffKopeks'], 'status': 'draft', 'date': '12.10.2026'}
        data['invoices'].append(invoice)
        return {'invoice': invoice, 'duplicate': False}

@router.post('/invoices/{invoice_id}/issue')
def issue_invoice(invoice_id: str, profile=Depends(require_roles('director', 'accountant'))):
    with edit_state() as state:
        invoice = next((i for i in ensure_finance(state)['invoices'] if i['id'] == invoice_id), None)
        if not invoice:
            raise HTTPException(404, 'Квитанция не найдена')
        if invoice['status'] == 'draft':
            invoice['status'] = 'issued'
            now = datetime.now(timezone.utc).isoformat()
            state['history'].insert(0, {'id': f'issue-{invoice_id}', 'title': 'Демонстрационная квитанция выдана', 'detail': f'{invoice["service"]} · {invoice["studentName"]}', 'time': now, 'actor': profile['name'], 'roles': ['director','accountant'], 'userIds': ['d1','ac1']})
            state['notifications'].insert(0, {'id': f'invoice-{invoice_id}', 'title': 'Новая квитанция за кружок', 'text': invoice['service'], 'time': now, 'read': False, 'roles': ['student'], 'userIds': [invoice['studentId']], 'readBy': []})
        return {'invoice': invoice}

@router.get('/invoices/{invoice_id}/pdf')
def invoice_pdf(invoice_id: str, profile=Depends(require_roles('director', 'accountant', 'student'))):
    with edit_state() as state:
        invoice = next((i for i in ensure_finance(state)['invoices'] if i['id'] == invoice_id), None)
        if not invoice:
            raise HTTPException(404, 'Квитанция не найдена')
        if profile['role'] == 'student' and (invoice['studentId'] != profile['userId'] or invoice['status'] not in ('issued', 'paid')):
            raise HTTPException(403, 'Нет доступа к квитанции')
        invoice = dict(invoice)
    from reportlab.pdfgen import canvas
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    font = Path(__file__).parent / 'assets' / 'Inter.ttf'
    if not font.exists():
        raise HTTPException(503, 'Шрифт PDF недоступен')
    if 'SchoolInter' not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont('SchoolInter', str(font)))
    out = BytesIO()
    c = canvas.Canvas(out, pagesize=(595, 842))
    c.setTitle('Демонстрационная квитанция — не для оплаты')
    c.setFillColorRGB(.13,.28,.96); c.roundRect(36,730,523,75,16,fill=1,stroke=0)
    c.setFillColorRGB(1,1,1); c.setFont('SchoolInter',22); c.drawString(56,770,'Идеяфикс')
    c.setFont('SchoolInter',11); c.drawString(56,748,'Дополнительное образование · образец документа')
    c.setFillColorRGB(.12,.16,.32); c.setFont('SchoolInter',18); c.drawString(40,682,'Квитанция '+invoice['id'].upper())
    lines = [('Ученик', invoice['studentName']),('Класс',invoice['className']),('Кружок',invoice['service']),('Период','Октябрь 2026'),('Статус','Выдана' if invoice['status']=='issued' else 'Черновик')]
    for j,(label,value) in enumerate(lines):
        y=634-j*47; c.setFont('SchoolInter',10); c.setFillColorRGB(.42,.47,.62); c.drawString(40,y,label)
        c.setFont('SchoolInter',12); c.setFillColorRGB(.12,.16,.32); c.drawString(170,y,value)
    k=invoice['amountKopeks']; amount=f'{k//100:,}'.replace(',',' ')+f',{k%100:02d} ₽'
    c.setFont('SchoolInter',24); c.drawString(40,344,amount)
    c.setFillColorRGB(.83,.15,.29); c.setFont('SchoolInter',15); c.drawString(40,280,'ДЕМО. НЕ ДЛЯ ОПЛАТЫ.')
    c.setFillColorRGB(.42,.47,.62); c.setFont('SchoolInter',10)
    for j,line in enumerate(['Вымышленные данные и демонстрационный тариф.','Банковские реквизиты отсутствуют. Платёж не создаётся.','Рабочую форму и правила расчёта утверждает школа.']):
        c.drawString(40,247-j*20,line)
    c.showPage();c.save()
    return Response(out.getvalue(), media_type='application/pdf', headers={'Content-Disposition':f'attachment; filename="{invoice_id}.pdf"','Cache-Control':'no-store'})
