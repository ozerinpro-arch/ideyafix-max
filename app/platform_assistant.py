"""Native MAX navigation over the independently running platform core."""
from pathlib import Path
import hashlib
import json
import re

from .platform_chat import ChatState
from .platform_navigation import cover_image, help_reply, home_reply, section_parent, section_reply

SCHEDULE={'schedule','calendar','preferences','requests','replacements','publish',
          'analytics','admin','exchange','restore','makeups','outcomes','notifications'}
LEARNING={'materials','learning','journal','consultations','clubs','recovery','ai'}

class PlatformAssistant:
    def __init__(self,runtime=None,*,nanogpt_config=None):
        from .platform_runtime import PlatformRuntime
        from .platform_schedule import ScheduleAssistant
        from .platform_learning import LearningAssistant
        from .platform_max_features import MaxFeatures
        self.runtime=runtime if runtime is not None else PlatformRuntime(nanogpt_config=nanogpt_config)
        self.router=ChatState(self.runtime.root/'router.sqlite3')
        conversations=ChatState(self.runtime.root/'conversations.sqlite3')
        self.handlers={'schedule':ScheduleAssistant(self.runtime,conversations),
                       'learning':LearningAssistant(self.runtime,conversations)}
        self._profiles={p['id']:p for p in self.runtime.profiles()}
        self.max_features=MaxFeatures(self.runtime)

    def profiles(self):
        profiles=self.runtime.profiles()
        self._profiles={p['id']:p for p in profiles}
        return profiles

    def claim_native_invite(self,capability,event_id):
        result=self.runtime.claim_native_invite(capability,event_id)
        if result.get('status')==200:self.profiles()
        return result

    def authorize(self,actor_id):
        return self.runtime.authorize(actor_id)

    def handle_max(self,actor_id,text,event_id,*,bound_profiles,channel_status,session_key=None):
        return self.max_features.handle(actor_id,text,event_id,bound_profiles=bound_profiles,
            channel_status=channel_status,session_key=session_key)

    def _denied(self):
        return {'text':'Личный доступ изменился или отозван. Обратитесь к администратору за новым доступом.','buttons':[],'accessDenied':True}

    def validate_reply(self,actor_id,reply):
        guards=reply.get('resourceGuards',[])
        def changed():
            return {'text':'Документ или доступ к материалу изменился. Откройте раздел заново и получите актуальные сведения.','buttons':[{'label':'Личный кабинет','command':'/home'}],'resourceDenied':True}
        def read(path):
            try:return self.runtime.request(actor_id,'GET',path)
            except Exception:return {'status':503}
        if not isinstance(guards,list)or len(guards)>10:return changed()
        for guard in guards:
            if not isinstance(guard,dict):return changed()
            if guard.get('kind')=='club_document':
                current=read('/api/student/clubs')
                if current.get('status')!=200:return changed()
                documents=(current.get('data')or{}).get('documents',[])
                if not any(row.get('id')==guard.get('documentId')and row.get('revision')==guard.get('revision')for row in documents):return changed()
            elif guard.get('kind')=='material_grant':
                identity=guard.get('grantId')
                if not isinstance(identity,str)or not identity.startswith('share_')or not all(c.isalnum()or c in '_-'for c in identity):return changed()
                current=read('/api/teacher/replacement-materials/grants/'+identity)
                if current.get('status')!=200:return changed()
            elif guard.get('kind')=='native_read':
                path=guard.get('path');expected=guard.get('fingerprint')
                if not isinstance(path,str)or not path.startswith('/api/')or len(path)>4096 or not isinstance(expected,str)or not re.fullmatch('[a-f0-9]{64}',expected):return changed()
                current=read(path)
                if current.get('status')!=200:return changed()
                projection=guard.get('projection')
                if projection is not None:
                    if projection!='teacher_preparation'or path!='/api/teacher/preparation':return changed()
                    data=current.get('data')
                    if not isinstance(data,dict):return changed()
                    current={**current,'data':{k:v for k,v in data.items()if k!='lessonRefreshInMs'}}
                try:
                    encoded=json.dumps({'data':current.get('data'),'file':current.get('file')},ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
                except (TypeError,ValueError):return changed()
                if hashlib.sha256(encoded).hexdigest()!=expected:return changed()
            elif guard.get('kind')=='schedule_notice':
                try:valid=self.max_features.validate_notice(actor_id,guard.get('intent'))
                except Exception:valid=False
                if not valid:return changed()
            else:return changed()
        return reply

    def handle(self,actor_id,text,event_id,*,session_key=None):
        # Cached conversations are private data too. Check the current native
        # membership before reading any router/handler receipt or stored page.
        if self.authorize(actor_id).get('status')!=200:return self._denied()
        self.profiles()
        scope=f'{session_key or "test"}:{actor_id}'
        command, _, argument = text.strip().partition(' ')
        namespace = command.lstrip('/').casefold()
        profile = self._profiles.get(actor_id, {})
        role = profile.get('role', '')
        def route(state):
            if namespace in {'start','home','platform','menu','more','today'}:
                state.clear()
                return home_reply(profile)
            if namespace == 'help':
                state.clear()
                return help_reply()
            if namespace == 'section':
                state.clear()
                return section_reply(role, argument.strip())
            if namespace in {'people','invite'}:
                if role not in {'director','platform_admin'}:
                    return {'text':'Приглашать пользователей может директор или администратор.','buttons':[{'label':'Мой кабинет','command':'/home'}]}
                allowed=[p for p in self._profiles.values()if p['id']!=actor_id and(profile['role']=='platform_admin'or p['role']in {'student','teacher'})]
                if namespace=='people':
                    return {'text':'Выберите вымышленный личный профиль для приглашения. Ссылка закрепит профиль и права; получатель откроет её в MAX.',
                        'buttons':[{'label':p['name'],'command':'/invite '+str(p['id'])}for p in allowed]+[{'label':'Личный кабинет','command':'/home'}]}
                target=next((p for p in allowed if str(p['id'])==argument),None)
                if target is None:return {'text':'Выберите профиль кнопкой из раздела приглашений.','buttons':[{'label':'Пользователи','command':'/people'}]}
                return {'text':'Приглашение для '+target['name'],'inviteActorId':target['id'],'buttons':[{'label':'Пользователи','command':'/people'}]}
            owner='schedule' if namespace in SCHEDULE else 'learning' if namespace in LEARNING else state.get('owner')
            if text.startswith('/') and namespace not in SCHEDULE|LEARNING|{'cancel'}:
                return None
            if owner not in self.handlers:
                return {'text':'Откройте личный кабинет командой /start или выберите раздел кнопкой.',
                        'buttons':[{'label':'Личный кабинет','command':'/start'}]}
            state['owner']=owner
            reply=self.handlers[owner].handle(actor_id,text,event_id,session_key=scope)
            if reply is None:
                return {'text':'Ввод не относится к открытому действию. Выберите раздел или вернитесь в личный кабинет.',
                        'buttons':[{'label':'Личный кабинет','command':'/start'}]}
            actions = reply.setdefault('buttons', [])
            for action in actions:
                if action.get('command') == '/home':
                    action['label'] = 'Главная'
            if not argument and namespace in SCHEDULE | LEARNING:
                parent = section_parent(role, '/' + namespace)
                if parent and not any(action.get('command') == parent for action in actions):
                    actions.append({'label': 'Назад', 'command': parent})
                if namespace in {'replacements', 'publish', 'analytics'}:
                    reply['cover'] = 'operations'
                elif namespace in {'materials', 'learning', 'journal', 'clubs'} and not reply.get('document'):
                    reply['cover'] = 'operations' if namespace == 'materials' and role in {'planner', 'director'} else 'study'
            if not any(action.get('command') == '/home' for action in actions):
                actions.append({'label': 'Главная', 'command': '/home'})
            return reply
        reply=self.router.run('platform-router',scope,event_id,text,route)
        if reply is None:
            return None
        reply = self.validate_reply(actor_id, reply)
        cover = reply.pop('cover', None)
        if cover and not reply.get('image'):
            image = cover_image(cover)
            if image is not None:
                reply['image'] = image
        return reply

    def handle_file(self,actor_id,document,event_id,*,session_key=None):
        if self.authorize(actor_id).get('status')!=200:return self._denied()
        scope=f'{session_key or "test"}:{actor_id}'
        state=self.router.peek('platform-router',scope)
        handler=self.handlers.get(state.get('owner'))
        if handler is None or not hasattr(handler,'handle_file'):
            return {'text':'Сначала выберите действие импорта или загрузки, затем отправьте файл.',
                    'buttons':[{'label':'Обмен данными','command':'/exchange'},{'label':'Личный кабинет','command':'/home'}]}
        reply=handler.handle_file(actor_id,document,event_id,session_key=scope)
        return self.validate_reply(actor_id,reply)if reply is not None else None

    def close(self):
        self.runtime.close()
