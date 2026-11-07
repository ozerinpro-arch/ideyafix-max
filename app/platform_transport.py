"""Native MAX keyboards, photos and private document transfer."""
import base64
from io import BytesIO
import json
import re
import time
from urllib.parse import urlsplit

import httpx
from PIL import Image
from .live_transport import LiveMaxClient
from .vendor.max_transport import API_ORIGIN, TransportOutcome, _identity, _text, _tls_context
from .vendor.max_wire import is_personal_recipient, opaque_payload
from .school_demo_links import ALLOWED_URLS

MAX_FILE_BYTES=8*1024*1024
MAX_IMAGE_BYTES=2*1024*1024
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


def image_bytes(image):
    if not isinstance(image, dict) or set(image) != {'name', 'contentType', 'base64'}:
        raise ValueError('invalid_platform_image')
    encoded = image['base64']
    if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError('platform_image_too_large')
    name, mime, raw = document_bytes(image)
    formats = {'image/jpeg': ('JPEG', ('.jpeg', '.jpg')), 'image/png': ('PNG', ('.png',))}
    if mime not in formats or not name.lower().endswith(formats[mime][1]) or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('invalid_platform_image')
    try:
        with Image.open(BytesIO(raw), formats=[formats[mime][0]]) as photo:
            if max(photo.size) > 4096 or photo.width * photo.height > 6_000_000:
                raise ValueError('platform_image_too_large')
            photo.verify()
    except (OSError, Image.DecompressionBombError):
        raise ValueError('invalid_platform_image') from None
    return name, mime, raw

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

    def _upload(self,document,kind='file'):
        name,mime,raw=image_bytes(document) if kind=='image' else document_bytes(document)
        if self._token.encode()in raw or self._token in name:
            return TransportOutcome('rejected','credential_in_document')
        try:
            with self._client.stream('POST',API_ORIGIN+'/uploads',params={'type':kind})as response:
                content=bounded_body(response)
                if response.status_code!=200 or len(content)>65536:
                    return TransportOutcome('rejected'if response.status_code<500 else'unknown',kind+'_upload_not_created',response.status_code)
                data=json.loads(content)
            url=media_url(data.get('url'))
            if self._token in url:return TransportOutcome('unknown','unsafe_media_url')
            with self._media.stream('POST',url,files={'data':(name,raw,mime)})as response:
                content=bounded_body(response)
                if response.status_code not in(200,201) or len(content)>65536:
                    return TransportOutcome('unknown',kind+'_upload_outcome_unknown',response.status_code)
                uploaded=json.loads(content)if content else{}
            if kind=='image':
                photos=uploaded.get('photos')
                if not isinstance(photos,dict) or len(photos)!=1:
                    return TransportOutcome('unknown','invalid_image_upload_receipt')
                photo=next(iter(photos.values()))
                token=photo.get('token') if isinstance(photo,dict) else None
            else:
                token=uploaded.get('token') or uploaded.get('file',{}).get('token') or data.get('token')
            if not _text(token,8192) or self._token in token:
                return TransportOutcome('unknown','invalid_'+kind+'_upload_receipt')
            return {'type':kind,'payload':{'token':token}}
        except Exception:
            return TransportOutcome('unknown',kind+'_upload_transport_error')

    def send_reply(self,chat_id,text,*,buttons=(),document=None,image=None,image_guard=None):
        image_unavailable=False
        try:
            address={'chat_id':_identity(chat_id)}
            if not _text(text,4000) or len(buttons)>40 or document is not None and image is not None:
                raise ValueError()
            attachments=[]
            rows=[]
            pair=[]
            for button in buttons:
                if not isinstance(button,dict) or not _text(button.get('label'),128):raise ValueError()
                if set(button) == {'label', 'url'} and isinstance(button['url'], str) and button['url'] in ALLOWED_URLS:
                    item = {'type':'link','text':button['label'],'url':button['url']}
                else:
                    item={'type':'callback','text':button['label'],'payload':opaque_payload(button.get('payload'))}
                wide = len(buttons) <= 30 and (len(button['label']) > 22 or item['type'] == 'link')
                if wide:
                    if pair:
                        rows.append(pair)
                        pair=[]
                    rows.append([item])
                else:
                    pair.append(item)
                    if len(pair) == 2:
                        rows.append(pair)
                        pair=[]
            if pair:
                rows.append(pair)
            if document is not None:
                file=self._upload(document)
                if isinstance(file,TransportOutcome):return file
                attachments.append(file)
                # Small generated documents still need processing before attach.
                time.sleep(.8)
            if image is not None:
                if image_guard is not None and not image_guard():
                    return TransportOutcome('rejected','image_access_changed')
                photo=self._upload(image,'image')
                if isinstance(photo,TransportOutcome):
                    if photo.code=='credential_in_document':
                        return photo
                    image_unavailable=True
                    text=text[:3950]+'\n\nФото временно недоступно.'
                else:
                    attachments.append(photo)
                    time.sleep(.8)
                if image_guard is not None and not image_guard():
                    return TransportOutcome('rejected','image_access_changed')
            if rows:
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
            return self._outcome('accepted','image_unavailable_text_delivered' if image_unavailable else 'provider_accepted',code,provider_message_id=mid)
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
