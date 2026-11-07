"""Optional fictional demonstration content, created only through native APIs."""
import argparse
import json
import os
from pathlib import Path

from .platform_runtime import PlatformRuntime


def prepare(runtime):
    marker=runtime.root/'demo-content-v1.json'
    if marker.exists():
        if json.loads(marker.read_text())!={'kind':'ideyafix-fictional-demo-content-v1'}:
            raise ValueError('foreign_demo_content_marker')
        return {'state':'already_prepared','allDomainDataFictional':True}
    def write(path,payload,event):
        result=runtime.request(710003,'POST',path,payload,'ideyafix-demo-content-v1:'+event)
        if result.get('status')!=200:raise RuntimeError('fictional_demo_native_action_not_confirmed')
        return result['data']
    write('/api/teacher/preparation/materials',{
        'kind':'lesson_plan','title':'Демо: сравниваем доли','topic':'Доли','className':'ИФ-7А',
        'subject':'Математика','objective':'Сравнить доли на одинаковых целых','durationMinutes':40,
        'content':'Нарисуйте два одинаковых круга. Разделите первый пополам, второй на три части. Сравните одну часть каждого круга и объясните вывод.',
        'answers':'При одинаковых целых одна вторая больше одной третьей. Ответ доступен преподавателю.'},'material')
    for student,title,question in [
        ('ifx-student-3','Славику: сравнить доли','Сравните 1/2 и 1/3. Объясните ответ одним предложением.'),
        ('ifx-student-1','Саше: найти половину','В коробке 12 карандашей. Сколько карандашей составляет половина?')]:
        write('/api/teacher/learning/assignments',{'studentId':student,'subject':'Математика','topic':'Доли',
            'gap':'Вымышленная учебная задача для проверки личного кабинета','title':title,
            'content':question,'answers':'Славик: 1/2 больше 1/3 при одинаковом целом. Саша: 6 карандашей.',
            'generate':False},student)
    for actor,own,foreign in [(710007,'Славику: сравнить доли','Саше: найти половину'),
                              (710004,'Саше: найти половину','Славику: сравнить доли')]:
        view=runtime.request(actor,'GET','/api/student/learning')
        encoded=json.dumps(view.get('data'),ensure_ascii=False)
        if view.get('status')!=200 or own not in encoded or foreign in encoded:
            raise RuntimeError('fictional_demo_personal_scope_not_verified')
    fd=os.open(marker,os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w')as output:json.dump({'kind':'ideyafix-fictional-demo-content-v1'},output)
    return {'state':'prepared','personalAssignments':2,'teacherMaterials':1,'allDomainDataFictional':True,'aiCalls':0}


def main():
    parser=argparse.ArgumentParser(description='Prepare optional fictional MAX demonstration content')
    parser.add_argument('--runtime',type=Path)
    options=parser.parse_args()
    runtime=PlatformRuntime(options.runtime)
    try:print(json.dumps(prepare(runtime)))
    finally:runtime.close()


if __name__=='__main__':main()
