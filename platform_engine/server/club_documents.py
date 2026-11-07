"""Synthetic-only school documents, rendered from confirmed charge snapshots."""
from base64 import b64decode, b64encode
from hashlib import sha256
from importlib import import_module
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from fastapi import HTTPException
from pydantic import Field

from . import school_clubs, staff_access
from .integration_settings import _flag
from .notice_jobs import authority
from .request_planning import digest


TEMPLATE_VERSION = 'synthetic-club-charge-v1'
MAX_PDF_BYTES = 256 * 1024
FILENAME = 'club-document.pdf'
ID = r'^charge-[a-f0-9]{32}$'
PERIOD = r'^(20\d{2}|2100)-(0[1-9]|1[0-2])$'


def preview_enabled(school, settings):
    if not _flag(os.environ, 'SHKOLA_CLUB_PDF_PREVIEW_ENABLED'):
        return False
    if (school != 'synthetic-school' or settings is None or settings.school != school
            or os.environ.get('SHKOLA_WORKSPACE_MODE') != 'school'):
        raise RuntimeError('Учебные PDF доступны только в изолированной тестовой школе с очередью')
    from .school_notice_jobs import SchoolNoticeSettings, _broker
    if os.environ.get('SHKOLA_DOMAIN_BACKEND') == 'postgresql' and type(settings) is not SchoolNoticeSettings:
        raise RuntimeError('Учебные PDF PostgreSQL требуют выбранные настройки школьной очереди')
    if isinstance(settings, SchoolNoticeSettings):
        from .school_postgres import SchoolPostgres, _private_directory
        try:
            if (type(settings) is not SchoolNoticeSettings or type(settings.storage) is not SchoolPostgres
                    or settings.storage.settings.school != school
                    or os.environ.get('SHKOLA_DOMAIN_BACKEND') != 'postgresql'
                    or not _flag(os.environ, 'SHKOLA_NOTICE_QUEUE_ENABLED')
                    or os.environ.get('SHKOLA_TEST_MODE') != 'isolated'):
                raise ValueError()
            roots = [Path('/tmp').resolve(), Path('/var/tmp').resolve()]
            if sys.platform == 'darwin':
                roots.append(Path('/private/var/folders'))
            _private_directory(settings.staff_path.parent)
            if (settings.staff_path.parent.stat().st_mode & 0o777 != 0o700
                    or not any(settings.staff_path.parent.is_relative_to(root) for root in roots)):
                raise ValueError()
            origin = urlsplit(settings.origin)
            if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.port is None
                    or _broker(settings.redis).hostname != '127.0.0.1'):
                raise ValueError()
            settings.validate()
            parameters = settings.storage.settings.connection_parameters()
            socket = Path(parameters['host'])
            _private_directory(socket)
            if socket.stat().st_mode & 0o777 != 0o700 or not any(socket.is_relative_to(root) for root in roots):
                raise ValueError()
            settings.storage.queue_identity()
            from .notice_jobs import DOCUMENT_TABLE, verify_job_schema
            with settings.connect() as connection:
                verify_job_schema(connection, DOCUMENT_TABLE, 'shkola_document_jobs_dispatch', runtime=True)
        except (OSError, ValueError, TypeError, AttributeError, RuntimeError, HTTPException):
            raise RuntimeError('Учебные PDF PostgreSQL требуют подтверждённый временный тестовый контур') from None
    else:
        from .test_isolation import isolated_database_paths
        if any(urlsplit(endpoint).hostname != '127.0.0.1' for endpoint in (settings.postgres, settings.redis)):
            raise RuntimeError('Учебные PDF доступны только в изолированной тестовой школе с очередью')
        domain, agent = isolated_database_paths()
        staff, bound = staff_access.configuration()
        if bound != school or staff.parent != domain.parent or agent.parent != domain.parent:
            raise RuntimeError('Учебные PDF требуют отдельного временного контура')
    try:
        import_module('reportlab.pdfgen.canvas')
        import_module('reportlab.platypus')
        _register_font()
    except Exception:
        raise RuntimeError('Учебные PDF требуют установленного ReportLab и штатного шрифта') from None
    return True


def checked_receipt(value, command):
    """Validate the existing stored PDF envelope without rendering or source access."""
    try:
        pdf = value['pdf']
        if value['commandHash'] != digest(command) or len(pdf) > 4 * ((MAX_PDF_BYTES+2)//3):
            raise ValueError()
        data = b64decode(pdf, validate=True)
        if (not data.startswith(b'%PDF-') or not data.rstrip().endswith(b'%%EOF')
                or len(data) > MAX_PDF_BYTES or sha256(data).hexdigest() != value['sha256']
                or b64encode(data).decode('ascii') != pdf):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(503, 'Не удалось проверить сохранённый учебный PDF') from None
    return {'status': 'ready', 'name': FILENAME, 'pdf': pdf, 'sha256': value['sha256']}


class DocumentInput(school_clubs.StrictBody):
    documentId: str = Field(pattern=ID)
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


class ChargeSnapshot(school_clubs.StrictBody):
    id: str = Field(pattern=ID)
    title: str = Field(min_length=1, max_length=160)
    period: str = Field(pattern=PERIOD)
    studentName: str = Field(min_length=1, max_length=160)
    className: str = Field(min_length=1, max_length=80)
    amountKopeks: int = Field(ge=0, le=school_clubs.MAX_TOTAL_KOPEKS)
    updatedAt: str = Field(min_length=1, max_length=80)

    def summary(self):
        return {'id': self.id, 'title': self.title, 'period': self.period,
                'revision': digest([TEMPLATE_VERSION, self.model_dump()])}


def _register_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font = 'SchoolInter'
    font_path = Path(__file__).parent / 'assets/Inter.ttf'
    if not font_path.is_file():
        raise FileNotFoundError('Шрифт учебного PDF недоступен')
    if font not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font, str(font_path)))
    return font


def render_pdf(source):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfgen import canvas
    from reportlab.platypus import Paragraph

    font = _register_font()
    stream = BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(595, 842), invariant=1, pageCompression=1)
    pdf.setTitle('Учебный документ по начислению')
    pdf.setFillColorRGB(.09, .17, .30)
    pdf.roundRect(44, 713, 507, 85, 12, stroke=0, fill=1)
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont(font, 22)
    pdf.drawString(62, 757, 'Идеяфикс')
    pdf.setFont(font, 12)
    pdf.drawString(62, 733, 'Учебный документ по начислению')
    value_style = ParagraphStyle('DocumentValue', fontName=font, fontSize=14, leading=18)
    y = 681
    for label, value in (('Кружок', source.title), ('Ученик', source.studentName + ' · ' + source.className),
                         ('Период', source.period), ('Код начисления', source.id)):
        pdf.setFillColorRGB(.34, .40, .50)
        pdf.setFont(font, 10)
        pdf.drawString(48, y, label)
        paragraph = Paragraph(escape(value), value_style)
        _, height = paragraph.wrap(498, 150)
        paragraph.drawOn(pdf, 48, y-height-8)
        y -= height+38
    rubles, kopeks = divmod(source.amountKopeks, 100)
    pdf.setFillColorRGB(.09, .17, .30)
    pdf.setFont(font, 23)
    pdf.drawString(48, y, f'{rubles:,}'.replace(',', ' ') + f',{kopeks:02d} ₽')
    pdf.setFont(font, 11)
    pdf.drawString(48, y-24, 'Статус начисления: подтверждено')
    pdf.setStrokeColorRGB(.77, .81, .86)
    pdf.line(48, 121, 547, 121)
    pdf.setFont(font, 12)
    pdf.drawString(48, 96, 'Учебный образец. Оплата по этому документу недоступна.')
    pdf.setFont(font, 10)
    pdf.drawString(48, 76, 'Подтверждение начисления и подтверждение поступления - разные записи.')
    pdf.showPage()
    pdf.save()
    value = stream.getvalue()
    if len(value) > MAX_PDF_BYTES:
        raise HTTPException(422, 'Превышен предел размера учебного PDF')
    return value


class ClubDocuments:
    def __init__(self, learning, jobs):
        self.learning = learning
        self.jobs = jobs

    def _sources(self, profile, *, connection=None):
        self.learning._actor(profile, {'student'})
        with self.learning.domain_scope(connection) as connection:
            student = self.learning._student(profile, self.learning._read(), connection=connection)
            school = self.learning.school
            try:
                data = school_clubs.store(school_clubs.read_school_state(connection))
                if any(not isinstance(data[key], list) or len(data[key]) > school_clubs.MAX_RECORDS
                       for key in ('programs', 'enrollments', 'charges')):
                    raise ValueError('Invalid club collections')
                def visible(row):
                    return (isinstance(row, dict) and row.get('archived') is False
                            and row.get('schoolId', school) == school)

                programs = {row['id'] for row in data['programs'] if visible(row)}
                entries = {(row['id'], row['programId']) for row in data['enrollments']
                           if visible(row) and row.get('studentId') == student['id'] and row.get('status') == 'active'}
                sources = {}
                for row in data['charges']:
                    if (not visible(row) or row.get('studentId') != student['id'] or row.get('status') != 'confirmed'
                            or row.get('programId') not in programs or (row.get('enrollmentId'), row.get('programId')) not in entries):
                        continue
                    item = ChargeSnapshot(id=row['id'], title=row['programTitle'], period=row['period'],
                                          studentName=student['name'], className=student['className'],
                                          amountKopeks=row['amountKopeks'], updatedAt=row['updatedAt'])
                    if item.id in sources:
                        raise ValueError('Duplicate charge identity')
                    sources[item.id] = item
            except (OSError, ValueError, TypeError, AttributeError, KeyError, sqlite3.Error):
                raise HTTPException(503, 'Не удалось сверить учебные документы') from None
            if self.learning._student(profile, self.learning._read(), connection=connection)['id'] != student['id']:
                raise HTTPException(403, 'Личная привязка ученика изменилась')
            return sources

    def listing(self, profile, *, connection=None):
        return [row.summary() for row in self._sources(profile, connection=connection).values()]

    def _source(self, request, profile):
        item = self._sources(profile).get(request.documentId)
        if item is None:
            raise HTTPException(404, 'Учебный документ недоступен')
        if item.summary()['revision'] != request.revision:
            raise HTTPException(409, 'Начисление изменилось. Обновите список документов.')
        return item

    def submit(self, request, profile, *, before_return=None):
        self._source(request, profile)
        command = self._command(request, profile)
        code, value = self.jobs.submit(command, profile)
        self._source(request, profile)
        if code == 200:
            value = checked_receipt(value, command)
        if before_return is not None:
            before_return()
        return code, value

    @staticmethod
    def _command(request, profile):
        body = request.model_dump()
        return {**body, 'requestId': 'clubdoc-' + digest([authority(profile), body])}

    def prepare(self, command, profile):
        request = DocumentInput(documentId=command['documentId'], revision=command['revision'])
        if command != self._command(request, profile):
            raise HTTPException(409, 'Версия учебного документа изменилась')
        value = render_pdf(self._source(request, profile))
        self._source(request, profile)
        return {'commandHash': digest(command), 'pdf': b64encode(value).decode('ascii'),
                'sha256': sha256(value).hexdigest()}
