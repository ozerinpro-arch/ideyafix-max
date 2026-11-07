"""Native document events: preserve strict personal-dialog wire validation."""
from copy import deepcopy
import hashlib
import json
from .vendor.max_wire import normalize_update,MaxWireError
from .platform_transport import media_url,MAX_FILE_BYTES

def normalize_platform_update(raw):
    if type(raw)is not dict:raise MaxWireError()
    original=json.dumps(raw,sort_keys=True,ensure_ascii=True,separators=(',',':'),allow_nan=False).encode()
    value=deepcopy(raw)
    attachment=None
    if value.get('update_type')=='message_created':
        message=value.get('message')
        if type(message)is not dict or type(message.get('body'))is not dict:raise MaxWireError()
        body=message['body']
        attachments=body.get('attachments')
        if attachments:
            if not isinstance(attachments,list) or len(attachments)!=1 or type(attachments[0])is not dict or attachments[0].get('type')!='file':
                raise MaxWireError('only_one_document_supported')
            item=attachments[0];payload=item.get('payload',{})
            if type(payload)is not dict:raise MaxWireError('invalid_file_payload')
            name=item.get('filename') or payload.get('filename')
            size=item.get('size',payload.get('size'))
            if not isinstance(name,str) or not 1<=len(name)<=160 or any(c in name for c in('/','\\','\r','\n')):
                raise MaxWireError('invalid_document_name')
            if size is not None and(type(size)is not int or not 0<size<=MAX_FILE_BYTES):
                raise MaxWireError('invalid_document_size')
            try:url=media_url(payload.get('url'))
            except ValueError:raise MaxWireError('invalid_document_url')from None
            attachment={'name':name,'url':url,'contentType':'application/octet-stream'}
            body['attachments']=[]
            if not isinstance(body.get('text'),str) or not body['text'].strip():body['text']='[Файл: '+name+']'
    native=normalize_update(json.dumps(value,ensure_ascii=True,allow_nan=False).encode()).as_dict()
    if attachment is not None:
        native['file']=attachment
        native['payloadHash']=hashlib.sha256(original).hexdigest()
    return native
