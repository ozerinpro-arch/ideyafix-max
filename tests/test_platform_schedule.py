"""Conversation contract tests; fake RPC isolates adapter from native domain tests."""
from copy import deepcopy
from datetime import date,timedelta
import base64
import hashlib
import json
from io import BytesIO
from pathlib import Path
import tempfile
import time
import zipfile
import unittest
from uuid import uuid4
import re
from urllib.parse import urlsplit

from fastapi import HTTPException
from app.platform_chat import ChatState
from app.platform_schedule import ScheduleAssistant


def synthetic_timetable_document():
    main='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/package/2006/relationships'
    doc='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    def cell(ref,text):return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
    parts={
        '[Content_Types].xml':'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>',
        '_rels/.rels':f'<Relationships xmlns="{rel}"><Relationship Id="r1" Type="{doc}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        'xl/workbook.xml':f'<workbook xmlns="{main}" xmlns:r="{doc}"><sheets><sheet name="Расписание" sheetId="1" r:id="s1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels':f'<Relationships xmlns="{rel}"><Relationship Id="s1" Type="{doc}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        'xl/worksheets/sheet1.xml':f'<worksheet xmlns="{main}"><sheetData><row r="1">'+cell('A1','Дни')+cell('B1','Уроки')+cell('C1','ИФ-7А')+'</row><row r="2">'+cell('A2','Понедельник')+cell('B2','1. 8:00 - 8:40')+cell('C2','Математика (101)\nМарина Демо')+'</row></sheetData></worksheet>'}
    stream=BytesIO()
    with zipfile.ZipFile(stream,'w')as archive:
        for name,text in parts.items():archive.writestr(zipfile.ZipInfo(name,(1980,1,1,0,0,0)),text)
    return {'name':'ideyafix-synthetic.xlsx','contentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','base64':base64.b64encode(stream.getvalue()).decode()}


def prepare_published_replacement(runtime, replacement_teacher='Лера Демо'):
    """Synthetic fixture for cross-adapter tests, using only real native API writes."""
    def request(method,path,body=None):
        result=runtime.request(710005,method,path,body,str(uuid4())if method!='GET'else None)
        if result['status']!=200:raise AssertionError((path,result['status'],result.get('data')))
        return result['data']
    config=request('GET','/api/planning/config');draft=request('GET','/api/timetable')
    groups={g['id']:g for g in config['input']['groups']};teachers={t['id']:t for t in config['input']['teachers']}
    for demand in config['input']['demands']:
        matches=[lesson for lesson in draft['lessons']if lesson['subject']==demand['subject']
                 and lesson['className']in {groups[g]['className']for g in demand['groupIds']}
                 and set(lesson['teachers'])=={teachers[t]['name']for t in demand['teacherIds']}]
        if len(matches)!=1:raise AssertionError('Synthetic source mapping is not unique')
        demand['sourceLessonIds']=[matches[0]['id']]
    request('POST','/api/planning/config',{'expectedRevision':config['revision'],
        'baseDraftRevision':draft['revision'],'input':config['input']})
    status=request('GET','/api/school/replacements')
    lesson=next(row for row in status['lessons']if row['subject']=='Математика'and 'Марина Демо'in row['teachers'])
    body={'date':lesson['date'],'lessonId':lesson['id'],'replacementTeacher':replacement_teacher,
        'reason':'Синтетическая межмодульная проверка','acknowledgeUnchecked':False,
        'expectedActiveVersion':status['activeVersion'],'expectedDraftRevision':status['draftRevision']}
    preview=request('POST','/api/school/replacements/preview',body)
    if not preview['ready']:raise AssertionError(preview['blockers'])
    request('POST','/api/school/replacements/apply',{'input':body,'requestId':str(uuid4()),'previewHash':preview['previewHash']})
    current=request('GET','/api/school/replacements');record=current['records'][-1]
    versions={'expectedActiveVersion':current['activeVersion'],'expectedDraftRevision':current['draftRevision']}
    preview=request('POST','/api/school/replacements/'+record['id']+'/publish-preview',versions)
    if not preview['ready']:raise AssertionError(preview['blockers'])
    published=request('POST','/api/school/replacements/'+record['id']+'/publish',{
        **versions,'requestId':str(uuid4()),'previewHash':preview['previewHash']})
    return {'record':published['record'],'publication':published['publication'],'originalLesson':lesson}


class Runtime:
    def __init__(self,root):
        self.root=Path(root);self.calls=[];self.responses=[]
        self.schema_value={'paths':{
            '/api/school/calendar':{'get':{}},
            '/api/school/calendar/config':{'patch':{'requestBody':{'content':{'application/json':{'schema':{'$ref':'#/components/schemas/Write'}}}}}},
            '/api/school/calendar/effective':{'get':{'parameters':[{'in':'query','name':'weekStart','required':True,'schema':{'type':'string','format':'date'}}]}},
            '/api/school/calendar/records/{kind}':{'post':{'parameters':[{'in':'path','name':'kind','required':True,'schema':{'type':'string','enum':['holidays','weeks']}}],
                'requestBody':{'content':{'application/json':{'schema':{'type':'object','required':['record'],
                    'properties':{'record':{'type':'object','required':['days'], 'properties':{'days':{'type':'array','items':{'type':'integer','minimum':0,'maximum':6}}}}}}}}}}},
            '/api/school/data-exchange/export':{'post':{'requestBody':{'content':{'application/json':{'schema':{'type':'object','properties':{}}}}}}},
            '/api/school/calendar/resolve-date':{'post':{'requestBody':{'content':{'application/json':{'schema':{'type':'object','required':['options'], 'properties':{'options':{'type':'object','additionalProperties':{'type':'string'}}}}}}}}}},
            'components':{'schemas':{'Write':{'type':'object','required':['expectedRevision','requestId','config'],'properties':{
                'expectedRevision':{'type':'integer','minimum':0},'requestId':{'type':'string'},
                'config':{'type':'object','required':['timezone','confirmed'],'properties':{'timezone':{'type':'string','minLength':1},'confirmed':{'type':'boolean'},'note':{'type':'string'}}}}}}}}
    def schema(self):return deepcopy(self.schema_value)
    def request(self,actor_id,method,path,payload=None,event_id=None):
        self.calls.append(dict(actor=actor_id,method=method,path=path,payload=deepcopy(payload),event=event_id))
        return self.responses.pop(0) if self.responses else {'status':200,'data':{'revision':3,'notice':'Сохранено'},'file':None}
    def upload(self,actor_id,method,path,document,event_id):
        return self.request(actor_id,method,path,{'file':document},event_id)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp');self.addCleanup(self.tmp.cleanup)
        self.runtime=Runtime(self.tmp.name);self.store=ChatState(Path(self.tmp.name)/'chat.sqlite3')
        self.bot=ScheduleAssistant(self.runtime,self.store)
    def send(self,text,actor=710005,event=None,session='test-dialog'):
        return self.bot.handle(actor,text,event or str(uuid4()),session_key=session)
    def start(self,ns,path,method):
        actions=[a for a in self.bot.actions if a['namespace']=='/'+ns]
        index=next(i+1 for i,a in enumerate(actions) if a['nativeEndpoint']==path and a['method']==method)
        return self.send('/'+ns+' action '+str(index))
    def configure(self):
        self.start('calendar','/api/school/calendar/config','PATCH')
        self.send('Europe/Moscow');self.send('да');return self.send('/calendar answer /skip')
    def test_typed_nested_write_requires_confirmation_and_replays_receipt_after_restart(self):
        reply=self.configure();self.assertIn('Подтвердить',str(reply['buttons']))
        self.assertFalse([c for c in self.runtime.calls if c['method']!='GET'])
        event=str(uuid4());result=self.send('/calendar confirm',event=event)
        call=self.runtime.calls[-1];self.assertEqual(call['method'],'PATCH');self.assertEqual(call['actor'],710005)
        self.assertEqual(call['payload']['config'],{'timezone':'Europe/Moscow','confirmed':True})
        self.assertEqual(call['payload']['expectedRevision'],3)
        self.assertEqual(len(call['payload']['requestId']),32)
        self.bot=ScheduleAssistant(self.runtime,ChatState(Path(self.tmp.name)/'chat.sqlite3'))
        self.assertEqual(self.send('/calendar confirm',event=event),result);self.assertEqual(len([c for c in self.runtime.calls if c['method']!='GET']),1)
        with self.assertRaises(HTTPException):self.send('/calendar menu',event=event)
    def test_read_query_uses_actual_native_path_and_no_mutation_confirmation(self):
        self.start('calendar','/api/school/calendar/effective','GET');self.send('2026-11-09')
        self.assertEqual(self.runtime.calls[-1]['path'],'/api/school/calendar/effective?weekStart=2026-11-09')
    def test_nested_list_collects_typed_items_without_json(self):
        self.start('calendar','/api/school/calendar/records/{kind}','POST')
        self.send('holidays');self.send('/calendar answer /add')
        self.assertIn('не подходит',self.send('9')['text'])
        self.send('1');self.send('/calendar answer /add');self.send('2');self.send('/calendar answer /done')
        self.assertFalse([c for c in self.runtime.calls if c['method']!='GET']);self.send('/calendar confirm')
        self.assertEqual(self.runtime.calls[-1]['payload'],{'record':{'days':[1,2]}})
        self.assertEqual(self.runtime.calls[-1]['path'],'/api/school/calendar/records/holidays')
    def test_dynamic_map_is_field_wizard_not_raw_json(self):
        self.start('calendar','/api/school/calendar/resolve-date','POST')
        self.send('/calendar answer /key message');self.send('Завтра');self.send('/calendar answer /done');self.send('/calendar confirm')
        self.assertEqual(self.runtime.calls[-1]['payload'],{'options':{'message':'Завтра'}})
    def test_forbidden_native_profile_response_not_reported_success(self):
        self.runtime.responses=[{'status':403,'data':{'detail':'Для этой роли действие недоступно'}}]
        reply=self.start('calendar','/api/school/calendar','GET')
        self.assertIn('не подтверждено',reply['text']);self.assertIn('роли',reply['text'])
    def test_failed_confirmation_keeps_same_native_event_for_retry(self):
        self.configure();self.runtime.responses=[{'status':409,'data':{'detail':'Результат пока неизвестен'}}]
        self.send('/calendar confirm');self.send('/calendar confirm')
        self.assertEqual(self.runtime.calls[-2]['event'],self.runtime.calls[-1]['event'])
    def test_rpc_lost_response_then_new_confirm_reuses_precommitted_intent(self):
        self.configure();original=self.runtime.request;seen=[]
        def lost(actor,method,path,payload=None,event_id=None):
            seen.append(event_id)
            if len(seen)==1:raise RuntimeError('synthetic_lost_after_native_commit')
            return original(actor,method,path,payload,event_id)
        self.runtime.request=lost
        with self.assertRaises(RuntimeError):self.send('/calendar confirm')
        self.bot=ScheduleAssistant(self.runtime,self.store)
        self.send('/calendar confirm')
        self.assertEqual(seen[0],seen[1]);self.assertTrue(seen[0].startswith('schedule-'))
    def test_export_document_preserves_rpc_file_metadata(self):
        file={'name':'demo.csv','contentType':'text/csv','base64':'YQo='}
        self.start('exchange','/api/school/data-exchange/export','POST')
        self.runtime.responses=[{'status':200,'data':None,'file':file}]
        self.assertEqual(self.send('/exchange confirm')['document'],file)
    def test_conversation_scope_isolated_by_actor_and_session(self):
        self.start('calendar','/api/school/calendar/config','PATCH')
        self.assertIsNone(self.send('Europe/Moscow',actor=710003))
        self.assertIsNone(self.send('Europe/Moscow',session='other-dialog'))
        self.assertIsNone(self.send('/learning'))
        self.assertFalse([c for c in self.runtime.calls if c['method']!='GET'])
    def test_native_ids_choices_come_from_current_actor_context(self):
        self.runtime.responses=[{'status':200,'data':{'revision':8}},{'status':200,'data':{'revision':8}}]
        self.start('calendar','/api/school/calendar','GET')
        reply=self.start('calendar','/api/school/calendar/config','PATCH')
        self.assertIn('Часовой пояс',reply['text'])
        self.send('Europe/Moscow');self.send('нет');self.send('/calendar answer /skip');self.send('/calendar confirm')
        self.assertEqual(self.runtime.calls[-1]['payload']['expectedRevision'],8)
    def test_cancel_restore_and_out_of_range_action_never_dispatch(self):
        self.start('calendar','/api/school/calendar/config','PATCH');self.send('/cancel')
        self.send('/calendar confirm');self.send('/calendar action 0');self.send('/restore')
        self.assertFalse([c for c in self.runtime.calls if c['method']!='GET'])

    def test_file_wait_confirmation_and_lost_receive_response_replay(self):
        self.runtime.schema_value['paths']['/api/timetable/import']={'post':{'parameters':[
            {'in':'query','name':'filename','required':True,'schema':{'type':'string'}},
            {'in':'query','name':'expectedRevision','required':True,'schema':{'type':'integer'}}]}}
        self.start('schedule','/api/timetable/import','POST')
        file={'name':'demo.xlsx','contentType':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','base64':base64.b64encode(b'synthetic-xlsx-native-parser-owns-validation').decode()}
        event=str(uuid4())
        reply=self.bot.handle_file(710005,file,event,session_key='test-dialog')
        self.assertEqual(self.bot.handle_file(710005,file,event,session_key='test-dialog'),reply)
        self.assertFalse([c for c in self.runtime.calls if c['method']!='GET'])
        self.send('/schedule confirm')
        call=self.runtime.calls[-1]
        self.assertEqual(call['payload'],{'file':file})
        self.assertIn('filename=demo.xlsx',call['path'])
        self.assertIn('expectedRevision=3',call['path'])

    def test_native_dynamic_record_model_replaces_arbitrary_map_prompt(self):
        self.runtime.schema_value['x-record-models']={'/api/school/calendar':{'weeks':{'type':'object','required':['weekStart'],'properties':{'weekStart':{'type':'string','format':'date'}}}}}
        self.start('calendar','/api/school/calendar/records/{kind}','POST');reply=self.send('weeks')
        self.assertIn('Начало недели',reply['text']);self.assertNotIn('/key',reply['text'])
        self.send('2026-11-09');self.send('/calendar confirm')
        self.assertEqual(self.runtime.calls[-1]['payload'],{'record':{'weekStart':'2026-11-09'}})

    def test_private_invite_handoff_staff_and_nested_student_never_renders_native_links(self):
        token='syntheticCapability123456789012345678901234567'
        cases=[('/api/staff/invites',{'nominationId':'nomination'},
                {'token':token,'path':'/__native/#invite='+token}),
               ('/api/staff/student-access/invitations',{'studentId':'student','username':'ifx.student'},
                {'invite':{'path':'/__native/#invite='+token,'docs':[{'url':'/__native/#invite='+token}]},'student':{'name':'Ученик Демо'}})]
        for path,body,response in cases:
            with self.subTest(path=path):
                self.runtime.schema_value['paths'][path]={'post':{'requestBody':{'content':{'application/json':{'schema':{
                    'type':'object','required':list(body),'properties':{k:{'type':'string'}for k in body}}}}}}}
                self.bot.spec=None
                self.start('admin',path,'POST')
                for value in body.values():self.send(value)
                self.runtime.responses=[{'status':200,'data':response,'file':None}]
                reply=self.send('/admin confirm')
                self.assertEqual(reply['nativeInviteToken'],token)
                self.assertNotIn(token,reply['text']);self.assertNotIn('/__native/',reply['text'])

    def test_read_fingerprint_survives_replay_and_all_paginated_result_replies(self):
        data={'items':[{'name':'Тестовая запись '+str(i),'text':'Только тест. '*30}for i in range(20)]}
        self.runtime.responses=[{'status':200,'data':data,'file':None}]
        actions=[a for a in self.bot.actions if a['namespace']=='/calendar']
        index=next(i+1 for i,a in enumerate(actions)if a['nativeEndpoint']=='/api/school/calendar'and a['method']=='GET')
        event=str(uuid4());text='/calendar action '+str(index)
        reply=self.send(text,event=event)
        expected=hashlib.sha256(json.dumps({'data':data,'file':None},sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
        guard={'kind':'native_read','path':'/api/school/calendar','fingerprint':expected}
        self.assertEqual(reply['resourceGuards'],[guard])
        self.assertEqual(self.send('/calendar result 2')['resourceGuards'],[guard])
        self.assertEqual(self.send(text,event=event)['resourceGuards'],[guard])
        self.configure();self.send('/calendar confirm')
        self.assertNotIn('resourceGuards',self.send('/calendar result 1'))




class NativeScheduleTests(unittest.TestCase):
    """Actual independent native child; its sockets and external file reads are denied."""
    observations=[]
    def setUp(self):
        from app.platform_runtime import PlatformRuntime
        self.tmp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ideyafix-native-schedule.')
        self.addCleanup(self.tmp.cleanup)
        self.runtime=PlatformRuntime(Path(self.tmp.name)/'engine');self.addCleanup(self.runtime.close)
        native_request=self.runtime.request
        templates=list(self.runtime.schema()['paths'])
        def observed(actor,method,path,payload=None,event_id=None):
            plain=urlsplit(path).path
            matches=[template for template in templates if re.fullmatch(re.sub(r'\\\{[^}]+\\\}',r'[^/]+',re.escape(template)),plain)]
            template=max(matches,key=lambda item:len(re.sub(r'\{[^}]+\}','',item)))if matches else 'unmatched-native-route'
            entry={'test':self._testMethodName,'method':method,'path':template}
            try:
                result=native_request(actor,method,path,payload,event_id)
                entry.update(status=result['status'],file=isinstance(result.get('file'),dict))
                return result
            except Exception as error:
                entry['exceptionType']=type(error).__name__;raise
            finally:self.observations.append(entry)
        self.runtime.request=observed
        self.bot=ScheduleAssistant(self.runtime)
    def send(self,text,actor=710003,event=None):
        return self.bot.handle(actor,text,event or str(uuid4()),session_key='native-dialog')
    def start(self,ns,path,method,actor=710003):
        actions=[a for a in self.bot.actions if a['namespace']=='/'+ns]
        index=next(i+1 for i,a in enumerate(actions)if a['nativeEndpoint']==path and a['method']==method)
        return self.send('/'+ns+' action '+str(index),actor)
    def fill(self,ns,values,actor=710003):
        for _ in range(150):
            form=self.bot.state_store.peek(f'schedule:{actor}','native-dialog').get('form')
            if form is None:return
            if form.get('ready'):return
            field=form['pending'][0];schema=self.bot._schema(field['schema']);value=values
            missing=False
            for key in field['path']:
                try:value=value[key]
                except (KeyError,IndexError,TypeError):missing=True;break
            if missing:
                self.assertFalse(field['required'],field['path']);answer='/skip'
            elif field.get('objectChoices') and not field.get('manual'):answer='/manual'
            elif 'anyOf'in schema:
                variants=schema['anyOf'];kind='null'if value is None else 'object'if isinstance(value,dict) else 'array'if isinstance(value,list) else 'boolean'if isinstance(value,bool) else 'integer'if isinstance(value,int) else 'string'
                def match(v):
                    resolved=self.bot._schema(v)
                    if resolved.get('type')!=kind:return False
                    discriminator=resolved.get('properties',{}).get('kind',{}).get('const')
                    return discriminator is None or isinstance(value,dict)and value.get('kind')==discriminator
                index=next(i+1 for i,v in enumerate(variants)if match(v))
                answer=str(index)
            elif schema.get('type')=='array':
                current=self.bot._get(form,field['path']);answer='/add'if len(current)<len(value) else '/done'
            elif schema.get('type')=='object'and schema.get('additionalProperties'):
                current=self.bot._get(form,field['path'])
                key=next((key for key in value if key not in current),None)
                answer='/key '+key if key is not None else '/done'
            else:answer='true'if value is True else 'false'if value is False else str(value)
            result=self.send('/'+ns+' answer '+answer,actor)
            self.assertNotIn('Значение не подходит',result['text'],field['path'])
        self.fail('Wizard did not finish')
    def test_native_teacher_absence_request_confirm_and_durable_replay(self):
        self.start('requests','/api/school/requests','POST')
        self.fill('requests',{'body':{'date':'2026-11-02','kind':'unavailability','lessonIds':[],
            'slot':None,'text':'Отсутствую на тестовом занятии. Все данные вымышлены.'}})
        before=self.runtime.request(710005,'GET','/api/school/requests')['data']['requests']
        self.assertEqual(before,[])
        event=str(uuid4());reply=self.send('/requests confirm',event=event)
        self.assertNotIn('не подтверждено',reply['text'])
        records=self.runtime.request(710005,'GET','/api/school/requests')['data']['requests']
        self.assertEqual(len(records),1);self.assertEqual(records[0]['kind'],'unavailability')
        self.bot=ScheduleAssistant(self.runtime)
        self.assertEqual(self.send('/requests confirm',event=event),reply)
        self.assertEqual(len(self.runtime.request(710005,'GET','/api/school/requests')['data']['requests']),1)
        denied=self.runtime.request(710004,'POST','/api/school/requests',{'requestId':str(uuid4()),'text':'Недопустимо'},str(uuid4()))
        self.assertEqual(denied['status'],403)

    def test_native_calendar_typed_create_archive_restore(self):
        self.start('calendar','/api/school/calendar/records/{kind}','POST',710005)
        self.fill('calendar',{'path':{'kind':'days'},'body':{'record':{
            'date':'2026-11-03','status':'closed','source':'Синтетический тест Идеяфикс','confirmed':True}}},710005)
        result=self.send('/calendar confirm',710005)
        self.assertNotIn('не подтверждено',result['text'])
        calendar=self.runtime.request(710005,'GET','/api/school/calendar')['data']
        row=next(r for r in calendar['records']['days']if r['date']=='2026-11-03')
        for action,archived in [('archive',True),('restore',False)]:
            self.start('calendar','/api/school/calendar/records/{kind}/{identity}/{action}','POST',710005)
            self.fill('calendar',{'path':{'kind':'days','identity':row['id'],'action':action},'body':{}},710005)
            result=self.send('/calendar confirm',710005)
            self.assertNotIn('не подтверждено',result['text'])
            calendar=self.runtime.request(710005,'GET','/api/school/calendar')['data']
            self.assertEqual(next(r for r in calendar['records']['days']if r['id']==row['id'])['archived'],archived)

    def test_native_owner_record_crud_preserves_staff_denial(self):
        self.start('admin','/api/school/records/{kind}','POST',710008)
        self.fill('admin',{'path':{'kind':'students'},'body':{'record':{'name':'Ирина Тестовая','className':'ИФ-7А'}}},710008)
        result=self.send('/admin confirm',710008)
        self.assertNotIn('не подтверждено',result['text'])
        state=self.runtime.request(710008,'GET','/api/school/workspace')['data']
        row=next(r for r in state['records']['students']if r['name']=='Ирина Тестовая')
        self.start('admin','/api/school/records/{kind}/{record_id}','PATCH',710008)
        self.fill('admin',{'path':{'kind':'students','record_id':row['id']},'body':{'record':{'name':'Ирина Тестовая','className':'ИФ-8А'}}},710008)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
        for action,archived in [('archive',True),('restore',False)]:
            self.start('admin','/api/school/records/{kind}/{record_id}/'+action,'POST',710008)
            self.fill('admin',{'path':{'kind':'students','record_id':row['id']},'body':{}},710008)
            self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
            current=self.runtime.request(710008,'GET','/api/school/workspace')['data']
            actual=next(r for r in current['records']['students']if r['id']==row['id'])
            self.assertEqual(actual['archived'],archived);self.assertEqual(actual['className'],'ИФ-8А')
        self.assertEqual(self.runtime.request(710005,'GET','/api/school/workspace')['status'],403)

    def test_native_replacement_preview_apply_and_publish_with_current_versions(self):
        config=self.runtime.request(710005,'GET','/api/planning/config')['data']
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        # Explicit synthetic source mapping is fixture preparation through the
        # actual config API, never a relaxed replacement guard or direct DB edit.
        groups={g['id']:g for g in config['input']['groups']}
        teachers={t['id']:t for t in config['input']['teachers']}
        for demand in config['input']['demands']:
            matches=[lesson for lesson in draft['lessons']if lesson['subject']==demand['subject']
                     and lesson['className']in {groups[g]['className']for g in demand['groupIds']}
                     and set(lesson['teachers'])=={teachers[t]['name']for t in demand['teacherIds']}]
            self.assertEqual(len(matches),1)
            demand['sourceLessonIds']=[matches[0]['id']]
        setup=self.runtime.request(710005,'POST','/api/planning/config',{
            'expectedRevision':config['revision'],'baseDraftRevision':draft['revision'],
            'input':config['input']},str(uuid4()))
        self.assertEqual(setup['status'],200)
        status=self.runtime.request(710005,'GET','/api/school/replacements')['data']
        lesson=next(row for row in status['lessons']if row['subject']=='Математика'and 'Марина Демо'in row['teachers'])
        proposal={'date':lesson['date'],'lessonId':lesson['id'],'replacementTeacher':'Лера Демо',
                  'reason':'Синтетическая замена Идеяфикс','acknowledgeUnchecked':False}
        self.start('replacements','/api/school/replacements/preview','POST',710005)
        self.fill('replacements',{'body':proposal},710005)
        result=self.send('/replacements confirm',710005)
        self.assertNotIn('не подтверждено',result['text'])
        preview=self.bot.state_store.peek('schedule:710005','native-dialog')['previews']['/api/school/replacements/preview']
        self.assertTrue(preview['ready'],preview.get('blockers'))
        self.start('replacements','/api/school/replacements/apply','POST',710005)
        self.fill('replacements',{'body':{'input':proposal}},710005)
        result=self.send('/replacements confirm',710005)
        self.assertNotIn('не подтверждено',result['text'])
        current=self.runtime.request(710005,'GET','/api/school/replacements')['data']
        row=current['records'][0]
        self.start('replacements','/api/school/replacements/{identifier}/publish-preview','POST',710005)
        self.fill('replacements',{'path':{'identifier':row['id']},'body':{}},710005)
        self.assertNotIn('не подтверждено',self.send('/replacements confirm',710005)['text'])
        self.start('replacements','/api/school/replacements/{identifier}/publish','POST',710005)
        self.fill('replacements',{'path':{'identifier':row['id']},'body':{}},710005)
        self.assertNotIn('не подтверждено',self.send('/replacements confirm',710005)['text'])
        publication=self.runtime.request(710005,'GET','/api/school/publications')['data']['active']
        self.assertGreater(publication['version'],status['activeVersion'])
        listing=self.start('replacements','/api/school/replacements','GET',710005)
        self.assertIn('Марина Демо → Лера Демо',listing['text'])
        self.assertIn('Опубликована',listing['text'])
        self.assertNotIn('Planning event id',listing['text'])
        self.assertEqual(listing['resourceGuards'][0]['path'],'/api/school/replacements')

    def test_native_teacher_preference_manager_preview_and_approval(self):
        own=self.runtime.request(710003,'GET','/api/teacher/preferences')['data']
        self.assertTrue(own['teacherIds'])
        preference={'kind':'unavailable','teacherId':own['teacherIds'][0],
            'startDate':'2026-11-02','endDate':'2026-11-02','weekdays':[0],
            'startTime':None,'endTime':None,'note':'Вымышленная недоступность для проверки.'}
        self.start('preferences','/api/teacher/preferences','POST')
        self.fill('preferences',{'body':{'preference':preference}})
        self.assertNotIn('не подтверждено',self.send('/preferences confirm')['text'])
        manager=self.runtime.request(710005,'GET','/api/school/teacher-preferences')['data']
        row=manager['records'][0]
        updated={**preference,'note':'Уточнённая вымышленная недоступность.'}
        self.start('preferences','/api/teacher/preferences/{identifier}','POST')
        self.fill('preferences',{'path':{'identifier':row['id']},'body':{'preference':updated}})
        self.assertNotIn('не подтверждено',self.send('/preferences confirm')['text'])
        self.start('preferences','/api/school/teacher-preferences/{identifier}/preview','POST',710005)
        self.fill('preferences',{'path':{'identifier':row['id']},'body':{}},710005)
        self.assertNotIn('не подтверждено',self.send('/preferences confirm',710005)['text'])
        self.start('preferences','/api/school/teacher-preferences/{identifier}/decision','POST',710005)
        self.fill('preferences',{'path':{'identifier':row['id']},'body':{'decision':'approve','reason':'Подтверждённое тестовое решение.'}},710005)
        self.assertNotIn('не подтверждено',self.send('/preferences confirm',710005)['text'])
        records=self.runtime.request(710003,'GET','/api/teacher/preferences')['data']['records']
        self.assertEqual(records[0]['status'],'approved');self.assertEqual(records[0]['calendarState'],'active')

    def test_native_prepare_notices_and_personal_acknowledgement(self):
        self.start('notifications','/api/school/schedule-notifications/prepare','POST',710005)
        self.fill('notifications',{'body':{}},710005)
        self.assertNotIn('не подтверждено',self.send('/notifications confirm',710005)['text'])
        notices=self.runtime.request(710003,'GET','/api/staff/schedule-notifications')['data']['items']
        self.assertTrue(notices)
        row=notices[0]
        self.start('notifications','/api/staff/schedule-notifications/{identifier}/acknowledge','POST')
        self.fill('notifications',{'path':{'identifier':row['id']},'body':{}})
        self.assertNotIn('не подтверждено',self.send('/notifications confirm')['text'])
        current=self.runtime.request(710003,'GET','/api/staff/schedule-notifications')['data']['items']
        self.assertEqual(current[0]['status'],'acknowledged')

    def test_native_exchange_csv_preview_continue_apply_and_export_document(self):
        content='name;className\r\nНина Вымышленная;ИФ-7А\r\n'
        self.start('exchange','/api/school/data-exchange/preview','POST',710005)
        self.fill('exchange',{'body':{'kind':'students','format':'csv','content':content}},710005)
        reply=self.send('/exchange confirm',710005)
        self.assertNotIn('не подтверждено',reply['text'])
        self.assertTrue(any(b['command']=='/exchange continue'for b in reply['buttons']))
        self.send('/exchange continue',710005)
        self.assertNotIn('не подтверждено',self.send('/exchange confirm',710005)['text'])
        workspace=self.runtime.request(710008,'GET','/api/school/workspace')['data']
        self.assertEqual(sum(r['name']=='Нина Вымышленная'for r in workspace['records']['students']),1)
        self.start('exchange','/api/school/data-exchange/export','POST',710005)
        self.fill('exchange',{'body':{'kind':'students','format':'csv'}},710005)
        reply=self.send('/exchange confirm',710005)
        self.assertIn('Нина Вымышленная',base64.b64decode(reply['document']['base64']).decode('utf-8-sig'))

    def test_native_restore_alias_cas_history_replay_and_student_denial(self):
        before=self.runtime.request(710005,'GET','/api/timetable')['data']
        history=self.runtime.request(710005,'GET','/api/timetable/history')['data']
        target=next(row['revision']for row in history if row['revision']!=before['revision'])
        historical=self.runtime.request(710005,'GET','/api/timetable/history/'+str(target))['data']
        self.start('restore','/api/timetable/restore','POST',710005)
        self.fill('restore',{'body':{'targetRevision':target}},710005)
        event=str(uuid4());reply=self.send('/restore confirm',710005,event)
        self.assertNotIn('не подтверждено',reply['text'])
        after=self.runtime.request(710005,'GET','/api/timetable')['data']
        self.assertEqual(after['revision'],before['revision']+1);self.assertEqual(after['lessons'],historical['lessons'])
        self.assertEqual(self.send('/restore confirm',710005,event),reply)
        stale=self.runtime.request(710005,'POST','/api/timetable/restore',{'expectedRevision':before['revision'],'targetRevision':target},str(uuid4()))
        self.assertEqual(stale['status'],409)
        denied=self.runtime.request(710004,'POST','/api/timetable/restore',{'expectedRevision':after['revision'],'targetRevision':target},str(uuid4()))
        self.assertEqual(denied['status'],403)

    def test_native_all_eight_generic_record_kinds_crud_validation_and_replay(self):
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        own_lesson=next(lesson for lesson in draft['lessons']if 'Марина Демо'in lesson['teachers'])
        examples={
            'students':{'name':'КРУД Ученик','className':'ИФ-7А'},
            'teacherProfiles':{'name':'КРУД Преподаватель','subject':'Математика'},
            'tasks':{'title':'КРУД Задача','description':'Только вымышленные данные'},
            'documents':{'title':'КРУД Документ','type':'Учебный','content':'Тест'},
            'assignments':{'title':'КРУД Задание','className':'ИФ-7А'},
            'materials':{'title':'КРУД Материал','type':'text','content':'Тестовый текст'},
            'absences':{'teacherName':'Марина Демо','day':own_lesson['day'],'reason':'КРУД Отсутствие'},
            'notifications':{'title':'КРУД Уведомление','text':'Тестовый реестр, не внешняя отправка'}}
        for kind,record in examples.items():
            with self.subTest(kind=kind):
                before=self.runtime.request(710008,'GET','/api/school/workspace')['data']
                invalid=self.runtime.request(710008,'POST','/api/school/records/'+kind,
                    {'expectedRevision':before['revision'],'record':{}},str(uuid4()))
                self.assertEqual(invalid['status'],422)
                self.start('admin','/api/school/records/{kind}','POST',710008)
                self.fill('admin',{'path':{'kind':kind},'body':{'record':record}},710008)
                event=str(uuid4());result=self.send('/admin confirm',710008,event)
                self.assertNotIn('не подтверждено',result['text'])
                self.assertEqual(self.send('/admin confirm',710008,event),result)
                state=self.runtime.request(710008,'GET','/api/school/workspace')['data']
                self.assertEqual(len(state['records'][kind]),len(before['records'][kind])+1)
                created=state['records'][kind][-1]
                changed={**record}
                key='name'if 'name'in record else 'title'if 'title'in record else 'reason'
                changed[key]=record[key]+' изменено'
                self.start('admin','/api/school/records/{kind}/{record_id}','PATCH',710008)
                self.fill('admin',{'path':{'kind':kind,'record_id':created['id']},'body':{'record':changed}},710008)
                self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
                for action,archived in [('archive',True),('restore',False)]:
                    self.start('admin','/api/school/records/{kind}/{record_id}/'+action,'POST',710008)
                    self.fill('admin',{'path':{'kind':kind,'record_id':created['id']},'body':{}},710008)
                    self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
                    final=self.runtime.request(710008,'GET','/api/school/workspace')['data']
                    row=next(r for r in final['records'][kind]if r['id']==created['id'])
                    self.assertEqual(row['archived'],archived);self.assertEqual(row[key],changed[key])

    def test_native_active_read_families_and_exact_schema_inventory(self):
        schema=self.runtime.schema()
        for action in self.bot.actions:
            with self.subTest(route=action['nativeEndpoint'],method=action['method']):
                self.assertIn(action['method'].lower(),schema['paths'].get(action['nativeEndpoint'],{}))
        cases={710005:[
            '/api/timetable','/api/timetable/history','/api/timetable/dated-draft',
            '/api/planning/config','/api/planning/readiness','/api/planning/template',
            '/api/planning/runs','/api/planning/questions','/api/planning/experimental-school',
            '/api/planning/sources','/api/planning/school-source-registry',
            '/api/schedule-agent/status','/api/schedule-agent/rules',
            '/api/school/calendar','/api/school/publications','/api/school/replacements',
            '/api/school/requests','/api/school/requests/status','/api/staff/directory',
            '/api/staff/absences','/api/school/schedule-notifications','/api/school/teacher-preferences',
            '/api/school/replacement-outcomes','/api/school/data-exchange/catalog',
            '/api/school/data-exchange/lesson-progress-references',
            '/api/launch/tasks','/api/launch/outbox','/api/launch/respondents'],
            710003:['/api/staff/my-schedule','/api/teacher/preferences',
                '/api/teacher/replacement-outcomes','/api/staff/schedule-notifications'],
            710008:['/api/school/workspace','/api/school/timetable']}
        for actor,paths in cases.items():
            for path in paths:
                with self.subTest(actor=actor,path=path):
                    result=self.runtime.request(actor,'GET',path)
                    self.assertEqual(result['status'],200,result.get('data'))
                    self.assertTrue(isinstance(result.get('data'),(dict,list))or isinstance(result.get('file'),dict))
        self.assertFalse(any(a['nativeEndpoint'].startswith('/api/finance')or a['nativeEndpoint']=='/api/state'for a in self.bot.actions))

    def test_native_replacement_outcome_report_manager_preview_and_decision(self):
        fixture=prepare_published_replacement(self.runtime)
        own=self.runtime.request(710001,'GET','/api/teacher/replacement-outcomes')['data']
        item=next(item for item in own['items']if item.get('target',{}).get('replacementId')==fixture['record']['id'])
        self.start('outcomes','/api/teacher/replacement-outcomes','POST',710001)
        self.fill('outcomes',{'body':{'target':item['target'],'kind':'not_conducted',
            'reason':'Синтетическое сообщение: занятие не проведено.'}},710001)
        self.assertNotIn('не подтверждено',self.send('/outcomes confirm',710001)['text'])
        identifier=fixture['record']['id']
        self.start('outcomes','/api/school/replacement-outcomes/{identifier}/preview','POST',710005)
        self.fill('outcomes',{'path':{'identifier':identifier},'body':{}},710005)
        reply=self.send('/outcomes confirm',710005)
        self.assertNotIn('не подтверждено',reply['text'])
        self.send('/outcomes continue',710005)
        self.fill('outcomes',{'body':{'decision':'approve','reason':'Подтверждённый тестовый итог.'}},710005)
        self.assertNotIn('не подтверждено',self.send('/outcomes confirm',710005)['text'])
        current=self.runtime.request(710001,'GET','/api/teacher/replacement-outcomes')['data']
        outcome=next(row['outcome']for row in current['items']if row.get('target',{}).get('replacementId')==identifier)
        self.assertEqual(outcome['active']['kind'],'not_conducted');self.assertIsNone(outcome['pending'])

    def test_native_staff_nomination_invite_approval_execution_and_guards(self):
        self.assertTrue(any(p['id']==710009 and p['role']=='access_admin'for p in self.runtime.profiles()))
        self.start('admin','/api/staff/nominations','POST',710005)
        self.fill('admin',{'body':{'username':'ifx.wizard.test','name':'Новый Демо','role':'planner'}},710005)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710005)['text'])
        directory=self.runtime.request(710005,'GET','/api/staff/directory')['data']
        nomination=next(row for row in directory['nominations']if row['username']=='ifx.wizard.test')
        self.start('admin','/api/staff/invites','POST',710009)
        self.fill('admin',{'body':{'nominationId':nomination['id']}},710009)
        reply=self.send('/admin confirm',710009)
        self.assertNotIn('не подтверждено',reply['text']);self.assertNotIn('/__native/',reply['text'])
        target=next(row for row in directory['members']if row['role']=='planner')
        director=next(row for row in directory['members']if row['role']=='director')
        denied=self.runtime.request(710009,'POST','/api/staff/members/'+target['id']+'/approve-revoke',{},str(uuid4()))
        self.assertEqual(denied['status'],403)
        self.start('admin','/api/staff/members/{member_id}/approve-revoke','POST',710005)
        self.fill('admin',{'path':{'member_id':target['id']}},710005)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710005)['text'])
        directory=self.runtime.request(710009,'GET','/api/staff/directory')['data']
        approval=next(row for row in directory['revocations']if row['memberId']==target['id'])
        self.start('admin','/api/staff/revocations/{approval_id}/execute','POST',710009)
        self.fill('admin',{'path':{'approval_id':approval['id']}},710009)
        event=str(uuid4());reply=self.send('/admin confirm',710009,event)
        self.assertNotIn('не подтверждено',reply['text'])
        self.assertEqual(self.send('/admin confirm',710009,event),reply)
        directory=self.runtime.request(710005,'GET','/api/staff/directory')['data']
        self.assertFalse(next(row for row in directory['members']if row['id']==target['id'])['enabled'])
        self.assertIn(self.runtime.request(710006,'GET','/api/staff/directory')['status'],{401,403})
        self_revoke=self.runtime.request(710005,'POST','/api/staff/members/'+director['id']+'/revoke',{},str(uuid4()))
        self.assertEqual(self_revoke['status'],409)

    def test_native_solver_proposal_approval_and_full_publication(self):
        self.start('schedule','/api/planning/config','POST',710005)
        state=self.bot.state_store.peek('schedule:710005','native-dialog')
        self.assertEqual(state['form']['pending'][0]['path'],['body','input'])
        self.send('/schedule answer /object 1',710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        period=self.runtime.request(710005,'GET','/api/school/publications')['data']['active']['periodStart']
        self.start('schedule','/api/planning/runs','POST',710005)
        self.fill('schedule',{'body':{'mode':'generate','timeLimitSeconds':1,'periodStart':period}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        run=self.runtime.request(710005,'GET','/api/planning/runs')['data']['runs'][0]
        for _ in range(100):
            if run['status']not in {'queued','running'}:break
            time.sleep(.03)
            run=self.runtime.request(710005,'GET','/api/planning/runs/'+run['id'])['data']
        self.assertIn(run['status'],{'optimal','feasible'},run.get('message'))
        self.assertTrue(self.runtime.request(710005,'GET','/api/planning/readiness?periodStart='+period)['data']['ready'])
        self.start('schedule','/api/planning/runs/{run_id}/projection','GET',710005)
        self.fill('schedule',{'path':{'run_id':run['id']}},710005)
        self.start('schedule','/api/planning/runs/{run_id}/propose','POST',710005)
        self.fill('schedule',{'path':{'run_id':run['id']}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        task=self.runtime.request(710005,'POST','/api/planning/runs/'+run['id']+'/propose',{},str(uuid4()))['data']
        self.start('schedule','/api/schedule-agent/tasks/{task_id}/decision','POST',710005)
        self.fill('schedule',{'path':{'task_id':task['id']},'body':{'decision':'approve'}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.start('publish','/api/school/publications/preview','POST',710005)
        self.fill('publish',{'body':{'runId':run['id']}},710005)
        reply=self.send('/publish confirm',710005)
        self.assertNotIn('не подтверждено',reply['text'])
        self.assertTrue(any(b['command']=='/publish continue'for b in reply['buttons']))
        self.send('/publish continue',710005)
        self.assertNotIn('не подтверждено',self.send('/publish confirm',710005)['text'])
        active=self.runtime.request(710005,'GET','/api/school/publications')['data']['active']
        self.assertEqual(active['runId'],run['id']);self.assertGreater(active['version'],1)

    def test_native_xlsx_preview_manual_import_editor_and_export(self):
        document=synthetic_timetable_document()
        self.start('schedule','/api/timetable/preview','POST',710005)
        reply=self.bot.handle_file(710005,document,str(uuid4()),session_key='native-dialog')
        self.assertIn('Подтвердить',str(reply['buttons']))
        preview=self.send('/schedule confirm',710005)
        self.assertNotIn('не подтверждено',preview['text'])
        self.start('schedule','/api/timetable/import','POST',710005)
        self.bot.handle_file(710005,document,str(uuid4()),session_key='native-dialog')
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        # Original native import allows a new source draft; the subsequently
        # edited lesson requires the explicit unconfirmed manual context.
        config=self.runtime.request(710005,'GET','/api/planning/config')['data']
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        config['input']['confirmed']=False
        changed=self.runtime.request(710005,'POST','/api/planning/config',{
            'expectedRevision':config['revision'],'baseDraftRevision':draft['revision'],'input':config['input']},str(uuid4()))
        self.assertEqual(changed['status'],200)
        imported=self.runtime.request(710005,'GET','/api/timetable')['data']
        self.assertEqual(len(imported['lessons']),1)
        lesson=imported['lessons'][0]
        editable={k:lesson[k]for k in ['className','day','slot','start','end','subject','teachers','rooms']}
        editable['rooms']=['Кабинет 202']
        self.start('schedule','/api/timetable/lessons/{lesson_id}','POST',710005)
        self.fill('schedule',{'path':{'lesson_id':lesson['id']},'body':{'lesson':editable}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        edited=self.runtime.request(710005,'GET','/api/timetable')['data']
        self.assertEqual(edited['lessons'][0]['rooms'],['Кабинет 202'])
        self.start('schedule','/api/timetable/export','POST',710005)
        self.fill('schedule',{'body':{'format':'xlsx'}},710005)
        exported=self.send('/schedule confirm',710005)['document']
        self.assertTrue(base64.b64decode(exported['base64']).startswith(b'PK'))
        self.assertEqual(self.runtime.request(710003,'POST','/api/timetable/lessons/'+lesson['id'],{
            'expectedRevision':edited['revision'],'lesson':editable},str(uuid4()))['status'],403)

    def test_native_experiment_cancel_and_verified_makeup_lifecycle(self):
        period=self.runtime.request(710005,'GET','/api/school/publications')['data']['active']['periodStart']
        self.start('schedule','/api/planning/experimental-school','POST',710005)
        self.fill('schedule',{'body':{'periodStart':period,'useWorkingAssumptions':True}},710005)
        reply=self.send('/schedule confirm',710005)
        self.assertNotIn('не подтверждено',reply['text'])
        candidate=self.runtime.request(710005,'GET','/api/planning/experimental-school')['data']['latest']
        for _ in range(200):
            if candidate['status']not in {'queued','running'}:break
            time.sleep(.05)
            candidate=self.runtime.request(710005,'GET','/api/planning/experimental-school/'+candidate['id'])['data']
        self.assertIn(candidate['status'],{'optimal','feasible'},candidate.get('message'))
        identifier=candidate['id']
        version=self.runtime.request(710005,'GET','/api/planning/experimental-school/'+identifier+'/version')['data']
        event=version['eventDetails'][0]['eventId']
        self.start('schedule','/api/planning/experimental-school/{experiment_id}/edits/preview','POST',710005)
        self.fill('schedule',{'path':{'experiment_id':identifier},'body':{'operations':[
            {'kind':'delete','eventId':event,'reason':'Тестовая отмена с учётом долга.'}]}},710005)
        preview=self.send('/schedule confirm',710005)
        self.assertTrue(any(b['command']=='/schedule continue'for b in preview['buttons']),preview['text'])
        self.send('/schedule continue',710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        ledger=self.runtime.request(710005,'GET','/api/planning/experimental-school/'+identifier+'/makeups')['data']
        self.assertEqual(ledger['pending'],1);debt=ledger['records'][0]
        self.start('makeups','/api/planning/experimental-school/{experiment_id}/makeups/preview','POST',710005)
        self.fill('makeups',{'path':{'experiment_id':identifier},'body':{'recordId':debt['id']}},710005)
        suggestion=self.send('/makeups confirm',710005)
        self.assertTrue(any(b['command']=='/makeups continue'for b in suggestion['buttons']),suggestion['text'])
        self.send('/makeups continue',710005)
        self.assertNotIn('не подтверждено',self.send('/makeups confirm',710005)['text'])
        final=self.runtime.request(710005,'GET','/api/planning/experimental-school/'+identifier+'/makeups')['data']
        self.assertEqual(final['pending'],0);self.assertEqual(final['scheduled'],1)
        self.assertEqual(len(final['records']),1)
        self.start('schedule','/api/planning/experimental-school/{experiment_id}/week-preview','POST',710005)
        self.fill('schedule',{'path':{'experiment_id':identifier},'body':{'startDate':period,'schoolDays':[0,1,2,3]}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.start('publish','/api/planning/experimental-school/{experiment_id}/approval','POST',710005)
        self.fill('publish',{'path':{'experiment_id':identifier},'body':{'acceptWorkingAssumptions':True}},710005)
        self.assertNotIn('не подтверждено',self.send('/publish confirm',710005)['text'])
        self.start('publish','/api/planning/experimental-school/{experiment_id}/publication/preview','POST',710005)
        self.fill('publish',{'path':{'experiment_id':identifier},'body':{}},710005)
        reply=self.send('/publish confirm',710005)
        self.assertTrue(any(b['command']=='/publish continue'for b in reply['buttons']),reply['text'])
        self.send('/publish continue',710005)
        self.assertNotIn('не подтверждено',self.send('/publish confirm',710005)['text'])
        published=self.runtime.request(710005,'GET','/api/school/publications')['data']['active']
        self.assertGreater(published['version'],1)
        self.assertEqual(published['runId'],identifier)

    def test_native_long_text_file_preserves_confirmation_and_limits(self):
        actor=710008
        self.start('admin','/api/school/records/{kind}','POST',actor)
        self.fill('admin',{'path':{'kind':'documents'},'body':{'record':{'title':'Длинный документ','type':'Тест','content':'Коротко'}}},actor)
        self.send('/admin change',actor)
        form=self.bot.state_store.peek(f'schedule:{actor}','native-dialog')['form']
        index=next(i+1 for i,p in enumerate(form['editPaths']) if p[-1]=='content')
        self.send('/admin edit '+str(index),actor)
        def upload(raw,event=None):return self.bot.handle_file(actor,{'name':'note.txt','contentType':'text/plain','base64':base64.b64encode(raw).decode()},event or str(uuid4()),session_key='native-dialog')
        before=self.bot.state_store.peek(f'schedule:{actor}','native-dialog')['form']['pending']
        self.assertIn('UTF-8',upload(b'\xff')['text'])
        self.assertIn('управляющие',upload(b'bad\x00text')['text'])
        self.assertIn('управляющие',upload('bad\u0085text'.encode())['text'])
        self.assertIn('не подходит',upload(('А'*20001).encode())['text'])
        self.assertEqual(self.bot.state_store.peek(f'schedule:{actor}','native-dialog')['form']['pending'],before)
        text='Д'*4500;event=str(uuid4());reply=upload(text.encode(),event)
        self.assertEqual(upload(text.encode(),event),reply)
        self.assertEqual(self.bot.state_store.peek(f'schedule:{actor}','native-dialog')['form']['body']['record']['content'],text)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',actor)['text'])
        records=self.runtime.request(actor,'GET','/api/school/workspace')['data']['records']['documents']
        saved=next(t for t in records if t['title']=='Длинный документ')
        self.assertEqual(saved['content'],text)

    def test_native_launch_local_task_question_reply_transition_and_recovery(self):
        self.start('schedule','/api/launch/status','GET',710005)
        self.start('schedule','/api/launch/tasks','POST',710005)
        self.fill('schedule',{'body':{'title':'Нативная подготовка Идеяфикс','message':'Проверить вымышленные исходные данные.',
            'deadlineAt':int(time.time()*1000)+3600000}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        tasks=self.runtime.request(710005,'GET','/api/launch/tasks')['data']['tasks']
        task=next(t for t in tasks if t['title']=='Нативная подготовка Идеяфикс')
        for action,state in [('prepare','blocked'),('retry','queued')]:
            self.start('schedule','/api/launch/tasks/{task_id}/transition','POST',710005)
            self.fill('schedule',{'path':{'task_id':task['id']},'body':{'action':action}},710005)
            self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
            detail=self.runtime.request(710005,'GET','/api/launch/tasks/'+task['id'])['data']
            self.assertEqual(detail['task']['state'],state)
        people=self.runtime.request(710005,'GET','/api/launch/respondents')['data']['respondents']
        marina=next(p for p in people if p['name']=='Марина Демо')
        self.start('schedule','/api/launch/tasks/{task_id}/questions','POST',710005)
        self.fill('schedule',{'path':{'task_id':task['id']},'body':{
            'respondentMembershipId':marina['membershipId'],'text':'Подтвердите тестовые исходные сведения.'}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        detail=self.runtime.request(710005,'GET','/api/launch/tasks/'+task['id'])['data']
        question=detail['questions'][0]
        self.assertIn(self.runtime.request(710001,'GET','/api/launch/tasks/'+task['id'])['status'],{403,404})
        self.start('schedule','/api/launch/tasks/{task_id}','GET')
        self.fill('schedule',{'path':{'task_id':task['id']}})
        self.start('schedule','/api/launch/questions/{question_id}/answer','POST')
        self.fill('schedule',{'path':{'question_id':question['id']},'body':{'text':'Подтверждено: всё вымышленное.'}})
        self.assertNotIn('не подтверждено',self.send('/schedule confirm')['text'])
        detail=self.runtime.request(710005,'GET','/api/launch/tasks/'+task['id'])['data']
        self.assertEqual(detail['questions'][0]['state'],'answered');self.assertEqual(detail['task']['state'],'queued')
        self.assertEqual(detail['task']['result']['externalCalls'],0)
        self.start('schedule','/api/launch/tasks/{task_id}/transition','POST',710005)
        self.fill('schedule',{'path':{'task_id':task['id']},'body':{'action':'invalidate'}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.start('schedule','/api/launch/recover','POST',710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.start('schedule','/api/launch/tasks','POST',710005)
        self.fill('schedule',{'body':{'title':'Отменяемая подготовка','message':'Только тестовая запись.',
            'deadlineAt':int(time.time()*1000)+3600000}},710005)
        self.send('/schedule confirm',710005)
        task=next(t for t in self.runtime.request(710005,'GET','/api/launch/tasks')['data']['tasks']if t['title']=='Отменяемая подготовка')
        self.start('schedule','/api/launch/tasks/{task_id}/transition','POST',710005)
        self.fill('schedule',{'path':{'task_id':task['id']},'body':{'action':'cancel'}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.assertEqual(self.runtime.request(710005,'GET','/api/launch/tasks/'+task['id'])['data']['task']['state'],'cancelled')

    def test_native_student_invitation_claim_binding_and_revocation(self):
        self.start('admin','/api/school/records/{kind}','POST',710008)
        self.fill('admin',{'path':{'kind':'students'},'body':{'record':{'name':'Новый Ученик Демо','className':'ИФ-7А'}}},710008)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
        students=self.runtime.request(710005,'GET','/api/staff/student-access')['data']['students']
        student=next(s for s in students if s['name']=='Новый Ученик Демо')
        self.start('admin','/api/staff/student-access/invitations','POST',710005)
        self.fill('admin',{'body':{'studentId':student['id'],'username':'ifx.new.wizard.student'}},710005)
        reply=self.send('/admin confirm',710005)
        self.assertIn('nativeInviteToken',reply);token=reply['nativeInviteToken']
        self.assertNotIn(token,reply['text']);self.assertNotIn('/__native/',reply['text'])
        claimed=self.runtime.claim_native_invite(token,'native-student-claim')
        self.assertEqual(claimed['status'],200);profile=claimed['data']['profile']
        self.assertEqual(profile['role'],'student');self.assertGreaterEqual(profile['id'],720001)
        self.start('admin','/api/staff/student-schedule-bindings','POST',710005)
        self.fill('admin',{'body':{'studentId':student['id'],'groupIds':[]}},710005)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710005)['text'])
        own=self.runtime.request(profile['id'],'GET','/api/student/schedule')
        self.assertEqual(own['status'],200);self.assertEqual(own['data']['student']['name'],'Новый Ученик Демо')
        self.assertTrue(own['data']['complete'])
        personal=self.start('schedule','/api/student/schedule','GET',profile['id'])
        self.assertIn('Новый Ученик Демо',personal['text'])
        self.assertNotIn('Вика Демо',personal['text'])
        denied=self.runtime.request(profile['id'],'GET','/api/staff/student-schedule-bindings?studentId='+student['id'])
        self.assertEqual(denied['status'],403)
        directory=self.runtime.request(710005,'GET','/api/staff/directory')['data']
        member=next(m for m in directory['members']if m['username']=='ifx.new.wizard.student')
        self.start('admin','/api/staff/members/{member_id}/revoke','POST',710005)
        self.fill('admin',{'path':{'member_id':member['id']}},710005)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710005)['text'])
        self.assertIn(self.runtime.request(profile['id'],'GET','/api/student/schedule')['status'],{401,403})

    def test_native_archived_student_cached_pages_require_current_scope_guard(self):
        reply=self.start('schedule','/api/student/schedule','GET',710004)
        self.assertIn('resourceGuards',reply)
        workspace=self.runtime.request(710008,'GET','/api/school/workspace')['data']
        student=next(s for s in workspace['records']['students']if s['name']=='Саша Демо')
        result=self.runtime.request(710008,'POST','/api/school/records/students/'+student['id']+'/archive',{
            'expectedRevision':workspace['revision']},str(uuid4()))
        self.assertEqual(result['status'],200)
        directory=self.runtime.request(710005,'GET','/api/staff/directory')['data']
        self.assertTrue(next(row for row in directory['members']if row['name']=='Саша Демо')['enabled'])
        self.assertEqual(self.runtime.authorize(710004)['status'],403)
        self.assertEqual(self.runtime.request(710004,'GET','/api/student/schedule')['status'],403)
        page=self.send('/schedule result 1',710004)
        self.assertEqual(page['resourceGuards'],reply['resourceGuards'])
        self.assertEqual(page['resourceGuards'][0]['path'],'/api/student/schedule')

    def test_native_owner_read_guard_changes_after_record_mutation_without_auth_revocation(self):
        reply=self.start('admin','/api/school/workspace','GET',710008)
        before=reply['resourceGuards'][0]['fingerprint']
        workspace=self.runtime.request(710008,'GET','/api/school/workspace')['data']
        changed=self.runtime.request(710008,'POST','/api/school/records/students',{
            'expectedRevision':workspace['revision'],'record':{'name':'Новый Синтетический','className':'ИФ-7А'}},str(uuid4()))
        self.assertEqual(changed['status'],200);self.assertEqual(self.runtime.authorize(710008)['status'],200)
        current=self.runtime.request(710008,'GET','/api/school/workspace')
        fingerprint=hashlib.sha256(json.dumps({'data':current['data'],'file':current.get('file')},sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode()).hexdigest()
        self.assertNotEqual(fingerprint,before)
        self.assertEqual(self.send('/admin result 1',710008)['resourceGuards'][0]['fingerprint'],before)

    def test_native_calendar_configuration_all_record_models_effective_and_relative_date(self):
        calendar=self.runtime.request(710005,'GET','/api/school/calendar')['data']
        config=deepcopy(calendar['config']);config['periodEnd']=(date.fromisoformat(config['periodEnd'])+timedelta(days=7)).isoformat()
        config['note']='Тестовое расширение календаря Идеяфикс.'
        self.start('calendar','/api/school/calendar/config','PATCH',710005)
        self.fill('calendar',{'body':{'config':config}},710005)
        self.assertNotIn('не подтверждено',self.send('/calendar confirm',710005)['text'])
        monday=date.fromisoformat(config['periodStart'])
        records={
            'weeks':{'weekStart':(monday+timedelta(days=28)).isoformat(),'schoolDays':[0,1,2,3,4],'type':'Тестовая неделя'},
            'days':{'date':(monday+timedelta(days=1)).isoformat(),'status':'school'},
            'exceptions':{'startDate':(monday+timedelta(days=4)).isoformat(),'endDate':(monday+timedelta(days=4)).isoformat(),
                'kind':'teacher_unavailable','targetId':'t1','startTime':'12:00','endTime':'13:00'},
            'rules':{'startDate':config['periodStart'],'endDate':config['periodEnd'],'kind':'room_unavailable',
                'targetId':'r1','startTime':'12:00','endTime':'13:00','weekdays':[4]}}
        for kind,record in records.items():
            with self.subTest(kind=kind):
                record.update(source='Явный синтетический источник Идеяфикс',confirmed=True)
                if kind=='rules':
                    self.start('calendar','/api/school/calendar/preview','POST',710005)
                    self.fill('calendar',{'body':{'record':record}},710005)
                    self.assertNotIn('не подтверждено',self.send('/calendar confirm',710005)['text'])
                self.start('calendar','/api/school/calendar/records/{kind}','POST',710005)
                self.fill('calendar',{'path':{'kind':kind},'body':{'record':record}},710005)
                self.assertNotIn('не подтверждено',self.send('/calendar confirm',710005)['text'])
        current=self.runtime.request(710005,'GET','/api/school/calendar')['data']
        day=current['records']['days'][0]
        self.start('calendar','/api/school/calendar/records/{kind}/{identity}','PATCH',710005)
        self.fill('calendar',{'path':{'kind':'days','identity':day['id']},'body':{'record':{
            'date':day['date'],'status':'closed','source':'Изменение тестового дня','confirmed':True}}},710005)
        self.assertNotIn('не подтверждено',self.send('/calendar confirm',710005)['text'])
        self.start('calendar','/api/school/calendar/effective','GET',710005)
        self.fill('calendar',{'query':{'weekStart':config['periodStart']}},710005)
        self.start('calendar','/api/school/calendar/resolve-date','POST',710005)
        self.fill('calendar',{'body':{'expression':'завтра','messageTimestamp':config['periodStart']+'T12:00:00+03:00'}},710005)
        resolved=self.send('/calendar confirm',710005)
        self.assertIn((monday+timedelta(days=1)).isoformat(),resolved['text'])

    def test_native_owner_settings_and_director_schedule_analytics(self):
        self.start('admin','/api/school/settings','PATCH',710008)
        self.fill('admin',{'body':{'settings':{'schoolName':'Идеяфикс — тестовый учебный центр',
            'academicYear':'2026/2027','note':'Все записи вымышлены.'}}},710008)
        self.assertNotIn('не подтверждено',self.send('/admin confirm',710008)['text'])
        state=self.runtime.request(710008,'GET','/api/school/workspace')['data']
        self.assertEqual(state['settings']['schoolName'],'Идеяфикс — тестовый учебный центр')
        reply=self.start('analytics','/api/school/analytics/schedule','GET',710005)
        self.assertNotIn('не подтверждено',reply['text']);self.assertIn('resourceGuards',reply)
        self.start('analytics','/api/school/analytics/director','GET',710005)
        self.fill('analytics',{'query':{'source':'published'}},710005)
        self.assertEqual(self.runtime.request(710005,'GET','/api/school/analytics/director?source=published')['status'],200)
        self.assertEqual(self.runtime.request(710003,'GET','/api/school/analytics/director')['status'],403)

    def test_native_school_request_addressed_question_reply_manual_proposal_and_decision(self):
        config=self.runtime.request(710005,'GET','/api/planning/config')['data']
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        config['input']['confirmed']=False
        self.assertEqual(self.runtime.request(710005,'POST','/api/planning/config',{
            'expectedRevision':config['revision'],'baseDraftRevision':draft['revision'],'input':config['input']},str(uuid4()))['status'],200)
        document=synthetic_timetable_document()
        imported=self.runtime.upload(710005,'POST','/api/timetable/import?filename=ideyafix-synthetic.xlsx&expectedRevision='+str(draft['revision']),document,str(uuid4()))
        self.assertEqual(imported['status'],200)
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        own=self.runtime.request(710003,'GET','/api/staff/my-schedule')['data']
        lesson=own['lessons'][0]
        full=next(row for row in draft['lessons']if row['className']==lesson['className']and row['subject']==lesson['subject'])
        editable={key:full[key]for key in ['className','day','slot','start','end','subject','teachers','rooms']}
        editable['rooms']=['Кабинет 202']
        target=(date.fromisoformat(own['periodStart'])+timedelta(days=lesson['day'])).isoformat()
        self.start('requests','/api/school/requests','POST')
        self.fill('requests',{'body':{'kind':'change_lesson','date':target,'lessonIds':[lesson['id']],
            'text':'Измените кабинет тестового занятия.'}})
        self.assertNotIn('не подтверждено',self.send('/requests confirm')['text'])
        request=self.runtime.request(710005,'GET','/api/school/requests')['data']['requests'][0]
        respondents=self.runtime.request(710005,'GET','/api/launch/respondents')['data']['respondents']
        marina=next(row for row in respondents if row['name']=='Марина Демо')
        self.start('requests','/api/school/requests/{ident}/question','POST',710005)
        self.fill('requests',{'path':{'ident':request['id']},'body':{
            'text':'Подтвердите тестовый кабинет.','respondentMembershipId':marina['membershipId']}},710005)
        self.assertNotIn('не подтверждено',self.send('/requests confirm',710005)['text'])
        self.start('requests','/api/school/requests/{ident}/reply','POST')
        self.fill('requests',{'path':{'ident':request['id']},'body':{'text':'Подтверждаю: кабинет 202 — вымышленный пример.',
            'fields':{'lesson':editable}}})
        self.assertNotIn('не подтверждено',self.send('/requests confirm')['text'])
        self.assertIn(self.runtime.request(710001,'GET','/api/school/requests/'+request['id'])['status'],{403,404})
        self.start('requests','/api/school/requests/{ident}/proposal','POST',710005)
        self.fill('requests',{'path':{'ident':request['id']},'body':{
            'action':'change','lessonId':full['id'],'lesson':editable,'targetWeekStart':own['periodStart']}},710005)
        self.assertNotIn('не подтверждено',self.send('/requests confirm',710005)['text'])
        self.start('requests','/api/school/requests/{ident}/decision','POST',710005)
        self.fill('requests',{'path':{'ident':request['id']},'body':{'decision':'approve'}},710005)
        self.assertNotIn('не подтверждено',self.send('/requests confirm',710005)['text'])
        result=self.runtime.request(710005,'GET','/api/school/requests/'+request['id'])['data']['request']
        self.assertEqual(result['status'],'applied')
        self.assertEqual(result['draftReceipt']['targetWeekStart'],own['periodStart'])

    def test_native_source_registry_typed_mapping_preview_and_import(self):
        self.start('schedule','/api/planning/school-source-registry','POST',710005)
        self.fill('schedule',{'body':{}},710005)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        registry=self.runtime.request(710005,'GET','/api/planning/school-source-registry')['data']
        self.assertTrue(registry['saved']);self.assertEqual(registry['revision'],1)
        sources=self.runtime.request(710005,'GET','/api/planning/sources')['data']
        config=self.runtime.request(710005,'GET','/api/planning/config')['data']
        draft=self.runtime.request(710005,'GET','/api/timetable')['data']
        groups={g['id']:g for g in config['input']['groups']};teachers={t['id']:t for t in config['input']['teachers']}
        mapping={'classMap':{},'groupMap':{},'teacherMap':{},'roomIds':{}}
        for cell in sources['package']['cells']:
            teacher=next(t for t in teachers.values()if t['name']==cell['teacher'])
            demand=next(d for d in config['input']['demands']if d['teacherIds']==[teacher['id']]
                and d['subject']==cell['normalizedSubjects'][0]['subject']
                and groups[d['groupIds'][0]]['className']==cell['classLabel'])
            mapping['classMap'][cell['classLabel']]=groups[demand['groupIds'][0]]['className']
            mapping['groupMap'][cell['sourceCell']]=demand['groupIds']
            mapping['teacherMap'][cell['teacher']]=teacher['id'];mapping['roomIds'][cell['sourceCell']]=demand['roomIds']
        # Start with the same explicit synthetic resources and no manual loads;
        # source import owns its new demands instead of silently duplicating old ones.
        base=deepcopy(config['input']);base.update(demands=[],baseline=[],pins=[],eventChains=[],confirmed=False)
        self.assertEqual(self.runtime.request(710005,'POST','/api/planning/config',{
            'expectedRevision':config['revision'],'baseDraftRevision':draft['revision'],'input':base},str(uuid4()))['status'],200)
        self.start('schedule','/api/planning/sources/preview','POST',710005)
        self.fill('schedule',{'body':{'periodStart':base['periodStart'],'resolutions':mapping}},710005)
        prepared=self.bot.state_store.peek('schedule:710005','native-dialog')['form']['body']['resolutions']
        for key in mapping:self.assertEqual(prepared.get(key),mapping[key],key)
        self.assertNotIn('не подтверждено',self.send('/schedule confirm',710005)['text'])
        self.start('schedule','/api/planning/sources/import','POST',710005)
        self.fill('schedule',{'body':{'periodStart':base['periodStart'],'resolutions':mapping}},710005)
        prepared=self.bot.state_store.peek('schedule:710005','native-dialog')['form']['body']['resolutions']
        for key in mapping:self.assertEqual(prepared.get(key),mapping[key],key)
        event=str(uuid4());reply=self.send('/schedule confirm',710005,event)
        self.assertNotIn('не подтверждено',reply['text'])
        self.assertEqual(self.send('/schedule confirm',710005,event),reply)
        saved=self.runtime.request(710005,'GET','/api/planning/sources')['data']
        self.assertEqual(saved['revision'],1)
        current=self.runtime.request(710005,'GET','/api/planning/config')['data']
        self.assertEqual(len(current['input']['demands']),3,saved.get('preview',{}).get('unresolved'));self.assertFalse(current['input']['confirmed'])


if __name__ == '__main__':
    unittest.main()
