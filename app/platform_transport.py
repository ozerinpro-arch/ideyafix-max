"""Native MAX keyboards and private document transfer for the full bot platform."""
import base64
import json
import re
import time
from urllib.parse import urlsplit

import httpx
from .live_transport import LiveMaxClient
from .vendor.max_transport import API_ORIGIN, TransportOutcome, _identity, _text, _tls_context
from .vendor.max_wire import is_personal_recipient, opaque_payload

MAX_FILE_BYTES=8*1024*1024
MEDIA_DOMAINS=('max.ru','oneme.ru','okcdn.ru','mycdn.me','vkuser.net')

def bounded_body(response,limit=65536):
    result=bytearray()
    for part in response.iter_bytes():
        result.extend(part)
        if len(result)>limit:raise ValueError('response_too_large')
    return bytes(result)

def media_url(value):
    if not isinstance(value,str) or len(value)>8192 or any(ord(c)<33 for c in value):
        raise ValueError('invalid_max_media_url')
    p=urlsplit(value)
    if (p.scheme!='https' or p.username is not None or p.password is not None or p.fragment
            or p.port not in (None,443) or not p.hostname or
            not any(p.hostname==d or p.hostname.endswith('.'+d) for d in MEDIA_DOMAINS)):
        raise ValueError('invalid_max_media_url')
    return value

def document_bytes(document):
    if not isinstance(document,dict):raise ValueError('invalid_platform_document')
    name=document.get('name');mime=document.get('contentType','application/octet-stream')
    if (not _text(name,160) or '/'in name or '\\'in name or '\r'in name or '\n'in name
            or not isinstance(mime,str) or not re.fullmatch(r'[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+(?:; *charset=[A-Za-z0-9-]+)?',mime)):
        raise ValueError('invalid_platform_document')
    try:raw=base64.b64decode(document['base64'],validate=True)
    except (ValueError,TypeError,KeyError):raise ValueError('invalid_platform_document')from None
    if not 0<len(raw)<=MAX_FILE_BYTES:raise ValueError('platform_document_too_large')
    return name,mime,raw

class PlatformMaxClient(LiveMaxClient):
    def __init__(self,*args,media_transport=None,**kwargs):
        super().__init__(*args,**kwargs)
        if media_transport is not None and type(media_transport)is not httpx.MockTransport:
            raise ValueError('invalid_media_test_transport')
        ca=kwargs.get('additional_ca_file',args[1]if len(args)>1 else None)
        context=_tls_context(ca)
        transport=media_transport if media_transport is not None else httpx.HTTPTransport(verify=context,trust_env=False,retries=0)
        # Credentials are never forwarded to an upload/download host.
        self._media=httpx.Client(verify=context,transport=transport,trust_env=False,follow_redirects=False,
            timeout=httpx.Timeout(25,connect=5))

    def get_updates(self,**kwargs):
        kwargs.setdefault('update_types',('bot_started','bot_stopped','message_created','message_callback'))
        return super().get_updates(**kwargs)

    def _upload(self,document):
        name,mime,raw=document_bytes(document)
        if self._token.encode()in raw or self._token in name:
            return TransportOutcome('rejected','credential_in_document')
        try:
            with self._client.stream('POST',API_ORIGIN+'/uploads',params={'type':'file'})as response:
                content=bounded_body(response)
                if response.status_code!=200 or len(content)>65536:
                    return TransportOutcome('rejected'if response.status_code<500 else'unknown','file_upload_not_created',response.status_code)
                data=json.loads(content)
            url=media_url(data.get('url'))
            if self._token in url:return TransportOutcome('unknown','unsafe_media_url')
            with self._media.stream('POST',url,files={'data':(name,raw,mime)})as response:
                content=bounded_body(response)
                if response.status_code not in(200,201) or len(content)>65536:
                    return TransportOutcome('unknown','file_upload_outcome_unknown',response.status_code)
                uploaded=json.loads(content)if content else{}
            token=uploaded.get('token') or uploaded.get('file',{}).get('token') or data.get('token')
            if not _text(token,8192) or self._token in token:
                return TransportOutcome('unknown','invalid_file_upload_receipt')
            return {'type':'file','payload':{'token':token}}
        except Exception:
            return TransportOutcome('unknown','file_upload_transport_error')

    def send_reply(self,chat_id,text,*,buttons=(),document=None):
        try:
            address={'chat_id':_identity(chat_id)}
            if not _text(text,4000) or len(buttons)>40:raise ValueError()
            attachments=[]
            if document is not None:
                file=self._upload(document)
                if isinstance(file,TransportOutcome):return file
                attachments.append(file)
                # Small generated documents still need processing before attach.
                time.sleep(.8)
            if buttons:
                rows=[]
                for button in buttons:
                    if not isinstance(button,dict) or not _text(button.get('label'),128):raise ValueError()
                    item={'type':'callback','text':button['label'],'payload':opaque_payload(button.get('payload'))}
                    if not rows or len(rows[-1])==2:rows.append([])
                    rows[-1].append(item)
                attachments.append({'type':'inline_keyboard','payload':{'buttons':rows}})
            body={'text':text}
            if attachments:body['attachments']=attachments
        except (ValueError,TypeError):return TransportOutcome('rejected','invalid_platform_reply')
        result=self._call('POST','/messages',params=address,body=body)
        if isinstance(result,TransportOutcome):return result
        code,raw=result
        try:
            message=raw['message'];mid=message['body']['mid'];recipient=message.get('recipient')
            if not _text(mid,200) or any(c.isspace()for c in mid):raise ValueError()
            if recipient is not None and(not is_personal_recipient(recipient) or
                recipient.get('chat_id')is not None and _identity(recipient['chat_id'])!=address['chat_id']):raise ValueError()
            return self._outcome('accepted','provider_accepted',code,provider_message_id=mid)
        except (ValueError,TypeError,KeyError):return TransportOutcome('unknown','malformed_message_receipt',code)

    def download_document(self,metadata):
        if not isinstance(metadata,dict):raise ValueError('invalid_native_document')
        name=metadata.get('name')
        if not _text(name,160) or '/'in name or '\\'in name:raise ValueError('invalid_native_document')
        url=media_url(metadata.get('url'))
        if self._token in url:raise ValueError('unsafe_media_url')
        with self._media.stream('GET',url)as response:
            if response.status_code!=200:raise ValueError('native_file_not_available')
            result=bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result)>MAX_FILE_BYTES:raise ValueError('native_file_too_large')
        return {'name':name,'contentType':metadata.get('contentType','application/octet-stream'),
                'base64':base64.b64encode(result).decode('ascii')}

    def answer_callback(self,callback_id):
        if not _text(callback_id,200):return False
        try:
            with self._client.stream('POST',API_ORIGIN+'/answers',params={'callback_id':callback_id},json={'notification':'Принято'})as response:
                bounded_body(response)
                return response.status_code==200
        except Exception:return False

    def close(self):
        if hasattr(self,'_media'):self._media.close()
        super().close()
