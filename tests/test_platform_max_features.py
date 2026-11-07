"""Current channel controls against real isolated native auth and notices."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.platform_max_features import INVENTORY, MaxFeatures, SCHEDULE_NOTICE_TEXT
from app.platform_runtime import PlatformRuntime
from tests.test_platform_schedule import prepare_published_replacement


class MaxFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-max-features-test.')
        self.addCleanup(self.temp.cleanup)
        self.runtime=PlatformRuntime(Path(self.temp.name))
        self.addCleanup(self.runtime.close)
        self.features=MaxFeatures(self.runtime)
        self.bound=[p for p in self.runtime.profiles() if p['id'] in {710003,710005,710001}]

    def say(self,text,actor=710005,event=None,bound=None,session=None):
        return self.features.handle(actor,text,event or str(uuid4()),
            bound_profiles=self.bound if bound is None else bound,
            channel_status={'bound':len(self.bound),'queued':0,'unknown':0,'token':'NEVER DISPLAY'},session_key=session)

    def native(self,actor,method,path,body=None,event=None):
        result=self.runtime.request(actor,method,path,body,event or (str(uuid4()) if method!='GET' else None))
        self.assertEqual(result['status'],200,result.get('data'))
        return result['data']

    def prepare(self):
        staged=self.say('/max prepare')
        self.assertIn('/max confirm',[b['command'] for b in staged['buttons']])
        self.say('/max confirm')
        return self.native(710003,'GET','/api/staff/schedule-notifications')['items'][0]

    def select(self):
        self.prepare()
        response=self.say('/max choose 710003')
        self.assertNotIn('scheduleNoticeIntent',response)
        self.assertIn(SCHEDULE_NOTICE_TEXT,response['text'])
        return self.say('/max confirm')

    def test_channel_status_is_safe_and_nonmax_command_is_unclaimed(self):
        self.assertIsNone(self.say('/schedule'))
        reply=self.say('/max status')
        self.assertIn('Long Polling',reply['text'])
        self.assertIn('подтверждена',reply['text'])
        self.assertNotIn('NEVER DISPLAY',json.dumps(reply))
        self.assertIn('/people',[b['command'] for b in self.say('/max link')['buttons']])
        self.assertNotIn('подтверждена',self.say('/max me',bound=[])['text'])
        self.assertEqual(len(INVENTORY),6)

    def test_unlink_needs_confirmation_and_new_confirmation_does_not_repeat(self):
        self.assertNotIn('unlinkMax',self.say('/max confirm',710003))
        self.assertNotIn('unlinkMax',self.say('/max revoke',710003))
        event='unlink-confirm-synthetic'
        first=self.say('/max confirm',710003,event)
        self.assertTrue(first['unlinkMax'])
        self.features=MaxFeatures(self.runtime)
        self.assertEqual(self.say('/max confirm',710003,event),first)
        self.assertNotIn('unlinkMax',self.say('/max confirm',710003))
        self.say('/max revoke',710003);self.say('/max cancel',710003)
        self.assertNotIn('unlinkMax',self.say('/max confirm',710003))

    def test_actual_native_prepare_only_confirmed_bound_teacher_target_and_replay(self):
        self.prepare()
        listing=self.say('/max notices')
        commands=[b['command'] for b in listing['buttons']]
        self.assertIn('/max choose 710003',commands)
        self.assertIn('/max choose 710001',commands)
        self.assertNotIn('/max choose 710004',commands)
        self.assertNotIn('scheduleNoticeIntent',self.say('/max choose 710004'))
        self.say('/max choose 710003')
        event='native-notice-confirm'
        response=self.say('/max confirm',event=event)
        intent=response['scheduleNoticeIntent']
        self.assertEqual(set(intent),{'targetActorId','publicationVersion','noticeId'})
        self.assertEqual(intent['targetActorId'],710003)
        self.assertTrue(self.features.validate_notice(710003,intent))
        self.assertFalse(self.features.validate_notice(True,intent))
        self.assertFalse(self.features.validate_notice(710001,intent))
        self.assertEqual(self.say('/max confirm',event=event),response)
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm'))
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm',event=event,bound=[]))
        self.assertNotIn('teachers',json.dumps(response))
        self.assertNotIn('lessons',json.dumps(response))

    def test_conversation_is_scoped_to_sender_chat_and_cached_list_rechecks_bindings(self):
        self.prepare()
        listing=self.say('/max notices',event='list-before-unbind')
        self.assertIn('/max choose 710003',[b['command'] for b in listing['buttons']])
        self.assertNotIn('/max choose 710003',[b['command'] for b in self.say('/max notices',event='list-before-unbind',bound=[])['buttons']])
        self.say('/max choose 710003',event='preview-before-unbind')
        self.assertNotIn('/max confirm',[b['command'] for b in self.say('/max choose 710003',event='preview-before-unbind',bound=[])['buttons']])
        self.say('/max revoke',710003,session='sender-chat-a')
        self.assertNotIn('unlinkMax',self.say('/max confirm',710003,session='sender-chat-b'))
        self.assertTrue(self.say('/max confirm',710003,session='sender-chat-a')['unlinkMax'])

    def test_role_and_forged_bound_profile_never_grant_notice_authority(self):
        self.prepare()
        for actor in [710004,710003,710006,710009]:
            for command in ['/max notices','/max prepare','/max choose 710003','/max confirm']:
                self.assertNotIn('scheduleNoticeIntent',self.say(command,actor))
        forged=[{'id':710003,'role':'teacher','name':'Не Марина'}]
        self.assertNotIn('/max choose 710003',[b['command'] for b in self.say('/max notices',bound=forged)['buttons']])
        # Native owner has no staff-manager membership: do not alias director.
        self.assertNotIn('scheduleNoticeIntent',self.say('/max notices',710008))

    def test_lost_native_prepare_reply_reuses_committed_operation_after_new_event(self):
        self.say('/max prepare')
        original=self.runtime.request;attempts=[]
        def lose(actor,method,path,payload=None,event_id=None):
            result=original(actor,method,path,payload,event_id)
            if method=='POST' and path.endswith('schedule-notifications/prepare'):
                attempts.append((payload,event_id))
                if len(attempts)==1:
                    raise RuntimeError('synthetic response lost after native commit')
            return result
        with patch.object(self.runtime,'request',side_effect=lose):
            with self.assertRaises(RuntimeError):
                self.say('/max confirm')
            self.features=MaxFeatures(self.runtime)
            response=self.say('/max confirm')
        self.assertIn('/max choose 710003',[b['command'] for b in response['buttons']])
        self.assertEqual(len(attempts),2);self.assertEqual(attempts[0],attempts[1])
        self.assertEqual(self.native(710005,'GET','/api/school/schedule-notifications')['revision'],1)

    def test_native_acknowledgement_invalidates_pending_and_replayed_notice(self):
        self.prepare();self.say('/max choose 710003')
        event='confirmed-before-ack'
        response=self.say('/max confirm',event=event);intent=response['scheduleNoticeIntent']
        self.say('/max choose 710003')
        item=self.native(710003,'GET','/api/staff/schedule-notifications')['items'][0]
        path='/api/staff/schedule-notifications/'+item['id']+'/acknowledge'
        body={'expectedRevision':item['revision']}
        acknowledged=self.native(710003,'POST',path,body,'native-ack-once')
        self.assertEqual(self.native(710003,'POST',path,body,'native-ack-once'),acknowledged)
        self.assertFalse(self.features.validate_notice(710003,intent))
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm',event=event))
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm'))
        self.assertNotIn('/max choose 710003',[b['command'] for b in self.say('/max notices')['buttons']])

    def test_actual_new_publication_invalidates_notice_before_send(self):
        response=self.select();intent=response['scheduleNoticeIntent']
        self.say('/max choose 710003')
        publication=prepare_published_replacement(self.runtime)['publication']
        self.assertGreater(publication['version'],intent['publicationVersion'])
        self.assertFalse(self.features.validate_notice(710003,intent))
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm'))

    def test_revoked_target_and_manager_are_rechecked_before_cached_private_intent(self):
        self.prepare();self.say('/max choose 710003')
        event='before-native-revoke'
        response=self.say('/max confirm',event=event);intent=response['scheduleNoticeIntent']
        member=self.native(710003,'GET','/api/staff/me')['profile']['membershipId']
        self.native(710005,'POST','/api/staff/members/'+member+'/revoke',{})
        self.assertFalse(self.features.validate_notice(710003,intent))
        self.assertNotIn('scheduleNoticeIntent',self.say('/max confirm',event=event))
        self.assertNotIn('unlinkMax',self.say('/max revoke',710003))


class MaxFeaturesListBoundaryTests(unittest.TestCase):
    """The native integration tests above prove authority; this tests UI scale."""
    def test_all_500_confirmed_teachers_preserved_and_delivery_overview_is_current(self):
        with tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-max-list-test.') as temporary:
            class Runtime:
                root=Path(temporary)
                def profiles(self):
                    return [{'id':710005,'role':'director','name':'Директор Демо'}]+[
                        {'id':720001+i,'role':'teacher','name':'Учитель Демо '+str(i)} for i in range(500)]
                def authorize(self,actor):
                    return {'status':200}
                def request(self,actor,method,path):
                    if method!='GET':
                        raise AssertionError('The list must never mutate native state')
                    if path=='/api/school/schedule-notifications':
                        return {'status':200,'data':{'revision':1,'publicationVersion':1}}
                    if path!='/api/staff/schedule-notifications':
                        raise AssertionError('Unexpected native read')
                    return {'status':200,'data':{'publicationVersion':1,'items':[{
                        'id':'notice_'+format(actor,'032x'),'publicationVersion':1,
                        'status':'available','text':SCHEDULE_NOTICE_TEXT}]}}
            runtime=Runtime();features=MaxFeatures(runtime)
            bound=runtime.profiles()[1:]
            states={
                'notice_'+format(720001,'032x'):'queued',
                'notice_'+format(720002,'032x'):'started',
                'notice_'+format(720003,'032x'):'accepted',
                'notice_'+format(720004,'032x'):'unknown',
                'notice_'+format(720005,'032x'):'rejected',
                'notice_'+format(720006,'032x'):'PRIVATE VALUE MUST NOT RENDER'}
            def read():
                return features.handle(710005,'/max notices','same-cached-list',
                    bound_profiles=bound,channel_status={'noticeStates':states},session_key='synthetic-chat')
            response=read()
            self.assertEqual(len(response['buttons']),500)
            self.assertEqual({b['command'] for b in response['buttons']},
                             {'/max choose '+str(p['id']) for p in bound})
            labels={b['command']:b['label'] for b in response['buttons']}
            for actor,label in [(720001,'В очереди'),(720002,'Отправка начата'),(720003,'Принято MAX'),
                                (720004,'Результат неизвестен'),(720005,'Отклонено')]:
                self.assertIn(label,labels['/max choose '+str(actor)])
            self.assertNotIn('PRIVATE VALUE MUST NOT RENDER',json.dumps(response))
            states['notice_'+format(720001,'032x')]='accepted'
            updated={b['command']:b['label'] for b in read()['buttons']}
            self.assertIn('Принято MAX',updated['/max choose 720001'])
            self.assertNotIn('В очереди',updated['/max choose 720001'])


if __name__=='__main__':
    unittest.main()
