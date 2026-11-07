"""Explicit first director provisioning. Never runs on server startup.
Only use after approval of school configuration and access assignment.
Password is entered in the terminal, never a command argument or environment.
"""
import argparse
from getpass import getpass
from .staff_access import bootstrap_director

def main():
 parser=argparse.ArgumentParser(description='Создать первого директора в явно настроенной пустой staff-БД')
 parser.add_argument('--username',required=True)
 parser.add_argument('--name',required=True)
 args=parser.parse_args()
 password=getpass('Личный пароль директора: ')
 if password!=getpass('Повторите пароль: '):parser.error('Пароли не совпадают')
 identifier=bootstrap_director(args.username,args.name,password)
 print('Первый директор создан. Membership ID:',identifier)
if __name__=='__main__':main()
