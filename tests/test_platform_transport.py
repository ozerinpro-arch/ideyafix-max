import base64
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
import httpx
from app.platform_transport import PlatformMaxClient,media_url
from app.platform_wire import normalize_platform_update
from app.vendor.max_wire import MaxWireError

class NativePlatformTransportTests(unittest.TestCase):
    def setUp(self):
        self.root=tempfile.TemporaryDirectory(dir='/private/tmp',prefix='ifx-media-test.')
        self.addCleanup(self.root.cleanup)
        self.token=Path(self.root.name)/'token.txt';self.token.write_text('SYNTHETIC-PLATFORM-ONLY');self.token.chmod(0o600)
        self.api=[];self.media=[]
        def api(request):
            self.api.append(request)
            if request.url.path=='/uploads':
                url='https://iu.oneme.ru/uploadImage?scope=test' if request.url.params['type']=='image' else 'https://files.max.ru/upload/private-test'
                return httpx.Response(200,json={'url':url})
            if request.url.path=='/answers':return httpx.Response(200,json={'success':True})
            return httpx.Response(200,json={'message':{'body':{'mid':'mid-fixture'},'recipient':{'chat_type':'dialog','chat_id':456}}})
        def media(request):
            self.media.append(request)
            self.assertNotIn('authorization',request.headers)
            if request.url.path=='/uploadImage':
                return httpx.Response(200,json={'photos':{'one':{'token':'synthetic-image-token'}}})
            return httpx.Response(200,json={'token':'synthetic-file-token'})if request.method=='POST'else httpx.Response(200,content=b'col\nvalue')
        self.client=PlatformMaxClient(self.token,transport=httpx.MockTransport(api),media_transport=httpx.MockTransport(media))
        self.addCleanup(self.client.close)

    def test_buttons_use_native_opaque_callbacks_without_network(self):
        with patch('socket.socket.connect',side_effect=AssertionError('No network')):
            result=self.client.send_reply(456,'Личный кабинет',buttons=[{'label':'Расписание','payload':'a'*43}])
        self.assertEqual(result.status,'accepted')
        body=json.loads(self.api[0].content)
        self.assertEqual(body['attachments'][0]['payload']['buttons'][0][0],{'type':'callback','text':'Расписание','payload':'a'*43})
        self.assertEqual(self.media,[])

    def test_document_upload_does_not_forward_bot_credentials(self):
        document={'name':'пример.pdf','contentType':'application/pdf','base64':base64.b64encode(b'%PDF-1.4 synthetic').decode()}
        with patch('time.sleep'):
            result=self.client.send_reply(456,'Документ готов',document=document)
        self.assertEqual(result.status,'accepted')
        self.assertEqual([r.url.path for r in self.api],['/uploads','/messages'])
        self.assertEqual(len(self.media),1)
        body=json.loads(self.api[-1].content)
        self.assertEqual(body['attachments'][0],{'type':'file','payload':{'token':'synthetic-file-token'}})

    def test_document_containing_credential_is_never_uploaded(self):
        document={'name':'текст.txt','contentType':'text/plain','base64':base64.b64encode(self.token.read_bytes()).decode()}
        result=self.client.send_reply(456,'Документ',document=document)
        self.assertEqual(result.status,'rejected');self.assertEqual(self.api,[]);self.assertEqual(self.media,[])

    def test_photo_and_buttons_share_a_message_and_access_is_rechecked(self):
        photo=Path(__file__).resolve().parents[1]/'app/assets/max-visual/home.jpg'
        image={'name':'home.jpg','contentType':'image/jpeg','base64':base64.b64encode(photo.read_bytes()).decode()}
        with patch('time.sleep'):
            result=self.client.send_reply(456,'Моё расписание',image=image,
                buttons=[{'label':'Неделя','payload':'a'*43}],image_guard=lambda:True)
        self.assertEqual(result.status,'accepted')
        self.assertEqual(self.api[0].url.params['type'],'image')
        self.assertEqual(len(self.media),1)
        body=json.loads(self.api[-1].content)
        self.assertEqual(body['text'],'Моё расписание')
        self.assertEqual(body['attachments'][0],{'type':'image','payload':{'token':'synthetic-image-token'}})
        self.assertEqual(body['attachments'][1]['type'],'inline_keyboard')
        self.api.clear()
        authority=iter((True,False))
        with patch('time.sleep'):
            denied=self.client.send_reply(456,'Моё расписание',image=image,image_guard=lambda:next(authority))
        self.assertEqual(denied.code,'image_access_changed')
        self.assertEqual([request.url.path for request in self.api],['/uploads'])

    def test_download_is_bounded_and_no_auth_on_media_host(self):
        result=self.client.download_document({'name':'таблица.csv','url':'https://files.max.ru/private-fixture'})
        self.assertEqual(base64.b64decode(result['base64']),b'col\nvalue')
        self.assertEqual(self.api,[])
        for url in ['http://files.max.ru/a','https://127.0.0.1/a','https://files.max.ru.evil.test/a','https://user:password@files.max.ru/a','https://files.max.ru:8443/a']:
            with self.assertRaises(ValueError):media_url(url)

    def test_native_file_event_preserves_personal_identity_and_raw_hash(self):
        raw={'update_type':'message_created','timestamp':100,'message':{'sender':{'user_id':123},
            'recipient':{'chat_type':'dialog','chat_id':456},'body':{'mid':'file-mid','attachments':[{'type':'file','filename':'данные.xlsx','size':20,'payload':{'url':'https://files.max.ru/private'}}]}}}
        event=normalize_platform_update(raw)
        self.assertEqual(event['senderId'],'123');self.assertEqual(event['chatId'],'456')
        self.assertEqual(event['file']['name'],'данные.xlsx')
        raw['message']['recipient']['chat_type']='chat'
        with self.assertRaises(MaxWireError):normalize_platform_update(raw)
        raw['message']['recipient']['chat_type']='dialog';raw['message']['body']['attachments']=[None]
        with self.assertRaises(MaxWireError):normalize_platform_update(raw)

    def test_callback_ack_is_fixed_origin_and_no_webhook_changes(self):
        self.assertTrue(self.client.answer_callback('fake-callback-id'))
        self.assertEqual(self.api[-1].url.host,'platform-api2.max.ru')
        self.assertEqual(self.api[-1].url.path,'/answers')
        self.assertEqual(self.client.register_subscription('https://example.test/','x').status,'rejected')

if __name__=='__main__':unittest.main()
