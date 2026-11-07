"""Operator-only connection checks. No credentials, profiles or provider bodies in output.

status and doctor are local. check-max performs read-only provider calls.
register-max-webhook is an explicit external change, never called on startup.
probe-ai uses a synthetic message and a durable caller-provided request ID.
"""
import argparse
from datetime import datetime, timezone
import json
import os
import sys

from .integration_settings import IntegrationSettings
from .school_integrations import IntegrationResources


def run(argv=None):
    parser = argparse.ArgumentParser(description='Идеяфикс: проверка подключений без вывода секретов')
    parser.add_argument('command', choices=['status', 'doctor', 'check-max', 'register-max-webhook', 'probe-ai', 'worker-once'])
    parser.add_argument('--request-id', default='')
    parser.add_argument('--confirm-register', action='store_true')
    args = parser.parse_args(argv)
    from .school_postgres import SchoolPostgres, SchoolPostgresSettings
    selected = SchoolPostgresSettings.from_environment(os.environ.get('SHKOLA_SCHOOL_ID', ''))
    if selected is not None:
        from .school_max import SchoolMaxResources, settings_for_school
        settings = settings_for_school(selected.school)
        def make_resources():
            return SchoolMaxResources(settings, SchoolPostgres(selected))
    else:
        settings = IntegrationSettings.from_environment(os.environ.get('SHKOLA_SCHOOL_ID', ''))
        def make_resources():
            return IntegrationResources(settings)
    if args.command == 'register-max-webhook' and not args.confirm_register:
        return {'result': 'BLOCKED', 'reason': 'Регистрация требует явного --confirm-register',
            'registrationAttempted': False, 'externalCalls': 0}
    if args.command == 'register-max-webhook' and settings.max_update_mode != 'webhook':
        return {'result': 'BLOCKED', 'reason': 'Выбран локальный Long Polling; Webhook не регистрируется',
            'registrationAttempted': False, 'externalCalls': 0}
    if args.command in {'status', 'doctor'}:
        resources = make_resources()
        try:
            with resources.bind():
                result = resources.status()
            ready = ((not settings.request_ai_requested or result['ai']['enabled'])
                and (not settings.max_requested or result['max']['enabled'])
                and (not settings.max_send_requested or result['max']['sendEnabled']))
            result['result'] = 'PASS' if ready else 'BLOCKED'
            result['externalCalls'] = 0
            return result
        finally:
            resources.close()
    if args.command == 'probe-ai':
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', args.request_id):
            return {'result': 'BLOCKED', 'reason': 'Нужен постоянный request-id для одной проверочной попытки', 'externalCalls': 0}
        resources = make_resources()
        try:
            if resources.extraction is None:
                return {'result': 'BLOCKED', 'reason': 'ИИ не настроен или выключен', 'externalCalls': 0}
            # Deliberately synthetic: this does not access a timetable or create a school request.
            actor = {'schoolId': settings.school_id, 'userId': 'operator-synthetic-probe'}
            text = 'Синтетическая проверка подключения: завтра не смогу провести третий урок.'
            payload = {'syntheticProbe': True, 'text': text}
            receipt = resources.extraction.prepare('probe', args.request_id, actor, payload, text, datetime.now(timezone.utc).isoformat())
            value = resources.extraction.resolve(receipt, 'probe', args.request_id, actor, payload)
            confirmed = value.status == 'completed' and value.advisory.get('kind') == 'unavailability' and value.advisory.get('slot') == 3
            return {'result': 'PASS' if confirmed else 'UNKNOWN',
                'extractionStatus': value.status, 'schoolDataUsed': False, 'scheduleChanged': False,
                'notice': 'Повтор того же request-id использует квитанцию; живое качество школьных ответов этим не доказано'}
        finally:
            resources.close()
    if args.command == 'worker-once':
        from .max_app import pump_once
        resources = make_resources()
        try:
            return {'result': 'PASS' if resources.max_runtime is not None else 'BLOCKED', **pump_once(resources)}
        finally:
            resources.close()
    if not settings.max_config_ready:
        return {'result': 'BLOCKED', 'reason': 'MAX не настроен или выключен', 'externalCalls': 0}
    from .max_transport import MaxClient
    with MaxClient(settings.max_token, additional_ca_file=settings.max_ca_file or None) as client:
        bot = client.get_me()
        if bot.status != 'accepted':
            return {'result': 'BLOCKED', 'reason': 'Проверка бота не подтверждена', 'providerOutcome': bot.status, 'code': bot.code}
        if bot.data.get('username') != settings.max_username:
            return {'result': 'BLOCKED', 'reason': 'Токен относится к другому боту'}
        if args.command == 'check-max':
            subscriptions = client.list_subscriptions()
            found = (subscriptions.status == 'accepted' and any(row['url'] == settings.max_webhook_url
                for row in subscriptions.data.get('subscriptions', [])))
            return {'result': 'PASS' if subscriptions.status == 'accepted' else 'BLOCKED',
                'botIdentityMatches': True, 'subscriptionPresent': found, 'providerOutcome': subscriptions.status,
                'notice': 'Чтение API не доказывает доставку или чтение сообщений'}
        if not args.confirm_register:
            return {'result': 'BLOCKED', 'reason': 'Регистрация требует явного --confirm-register', 'registrationAttempted': False}
        if selected is not None:
            outcome = client.register_subscription(settings.max_webhook_url, settings.max_secret,
                update_types=('bot_started', 'bot_stopped'))
        else:
            outcome = client.register_subscription(settings.max_webhook_url, settings.max_secret)
        return {'result': 'PASS' if outcome.status == 'accepted' else 'UNKNOWN' if outcome.status == 'unknown' else 'BLOCKED',
            'registrationAttempted': True, 'providerOutcome': outcome.status, 'code': outcome.code,
            'notice': 'При неизвестном исходе сначала check-max; автоматического повтора нет'}


def main():
    os.environ['PYTHON_DOTENV_DISABLED'] = '1'
    try:
        result = run()
    except Exception:
        result = {'result': 'BLOCKED', 'reason': 'Не удалось безопасно проверить подключение; проверьте конфигурацию'}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get('result') == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
