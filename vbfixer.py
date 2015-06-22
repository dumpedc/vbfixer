# -*- coding: utf-8 -*-

import click
import codecs
import datetime
import difflib
import magic
import os
from pygments.lexers.php import PhpLexer
from pygments.token import Token


class WontFix(Exception):
    pass


# fix_counts = {'alarms': 0, 'refs': 0}


class PHPFixer(object):
    VBULLETIN_GLOBALS = ('$vbulletin', '$db',)
    all_inspections = (
        'assign_by_reference',  # Исправляет deprecation warning
    )

    def __init__(self, inspections=None, charset='utf-8'):
        self.charset = charset
        self.inspections = (
            self.all_inspections if inspections is None else filter(lambda x: x in self.all_inspections, inspections)
        )

    def detect_encoding(self, path):
        with open(path, 'rb') as f:
            blob = f.read()
            m = magic.Magic(mime_encoding=True)
            return m.from_buffer(blob)

    def fix(self, path, fixer=lambda base, new, to_replace, path, encoding: None):
        inline_lexer = PhpLexer(startinline=True)
        inspections = [
            {
                'process': getattr(self, 'process_{}'.format(inspection)),
                'context_getter': getattr(self, 'get_context_{}'.format(inspection), self.get_context)
            } for inspection in self.inspections
        ]
        base_lines = []
        fixed_lines = []
        to_replace = []
        try:
            encoding = self.charset or self.detect_encoding(path)
            with codecs.open(path, 'r', encoding) as f:
                for l_num, line in enumerate(f.readlines()):
                    base_lines.append(line)
                    for inspection in inspections:
                        context = inspection['context_getter'](line)

                        for token, text in inline_lexer.get_tokens(line):
                            if token is Token.Punctuation:
                                for t in list(text):
                                    context = inspection['process'](token, t, context)
                            else:
                                context = inspection['process'](token, text, context)
                        if line != context['line']:
                            to_replace.append(l_num)
                        line = context['line']
                    fixed_lines.append(line)
        except LookupError:
            raise WontFix('Encoding problem')
        if to_replace:
            fixer(base_lines, fixed_lines, to_replace, path, encoding)
        # print base_lines
        # print fixed_lines

    def get_context(self, line):
        return {'line': line}

    def get_context_assign_by_reference(self, line):
        return {
            'last_function': None, 'in_function_call': False, 'is_assign': False, 'assign_text': None,
            'is_amp': False, 'stack': [], 'line': line
        }

    def process_assign_by_reference(self, token, text, context):
        if token is Token.Comment.Preproc:
            return context
        # if context['in_function_call']:
        #     print u'Находимся внутри вызова функции', context['stack'][-1], text
        # if context['is_assign']:
        #     print context['line']
        #     print token, (text,)
        is_variable = token is Token.Name.Variable
        is_keyword = token is Token.Keyword
        is_name = token is Token.Name.Other
        is_operator = token is Token.Operator
        is_start_bracket = token is Token.Punctuation and text.startswith('(')
        is_ending_bracket = token is Token.Punctuation and text.startswith(')')
        is_endline = token is Token.Punctuation and text.startswith(';')
        is_assign_new = context['is_assign'] and is_keyword and text == 'new'
        is_object_by_ref = context['is_amp'] and is_variable and text in self.VBULLETIN_GLOBALS
        if context['is_assign']:
            context['assign_text'].append(text)
        context['is_assign'] = (context['is_assign'] and token is Token.Text) or (is_operator and text == '=&')
        if is_operator and text == '=&':
            context['assign_text'] = [text]
        context['is_amp'] = is_operator and text == '&'
        if is_assign_new:
            # print u'Алярма', context['line']
            context['line'] = context['line'].replace(u''.join(context['assign_text']), '= new')
            # fix_counts['alarms'] += 1
        if is_object_by_ref:
            # print u'Объект по ссылке', context['line']
            context['line'] = context['line'].replace(u'&{}'.format(text), text)
            # fix_counts['refs'] += 1
        # if is_endline:
        #     print u'Завершение строки', text
        # if is_variable:
        #     print u'Переменная', text
        # if is_keyword:
        #     print u'Зарегзервированное слово языка', text
        # if is_name:
        #     print u'Класс, функция или константа', text
        # if is_operator:
        #     print u'Оператор', text, context['is_assign']
        if is_start_bracket:
            # print u'Открыта открывающая скобка', text
            if context['last_function'] is not None:
                # print u'Начался вызов функции', context['last_function']
                context['in_function_call'] = True
                context['stack'].append(context['last_function'])
        if is_ending_bracket:
            # print u'Открыта закрвающая скобка', text
            if context['in_function_call']:
                # print u'Закончился вызов функции', context['stack'][-1]
                context['stack'].pop()
                context['in_function_call'] = bool(context['stack'])
        if is_keyword or is_name:
            context['last_function'] = text
        # print token, (text,)
        return context


# В общем случае это далеко не всегда определяется исключительно расширением файла, но нас не интересует
is_php = lambda path: os.path.basename(path).rsplit('.', 1)[-1] == 'php'
# Шоткат до стилизованного вывода click
styled = lambda text, color: click.echo(click.style(text, fg=color))

def click_fixer(base, new, to_replace, path, encoding):
    total = list(base)
    replaced = False
    for l_num in to_replace:
        styled('#######################', 'yellow')
        styled(u'# Текущий вариант {}:{}'.format(path, l_num), 'yellow')
        styled('#######################', 'yellow')
        click.echo(u''.join(base[l_num - 2:l_num]))
        styled(base[l_num], 'red')
        click.echo(u''.join(base[l_num + 1: l_num + 2]))
        styled('#######################', 'yellow')
        styled(u'# Предлагаемая замена', 'yellow')
        styled('#######################', 'yellow')
        click.echo(u''.join(new[l_num - 2:l_num]))
        styled(new[l_num], 'red')
        click.echo(u''.join(new[l_num + 1: l_num + 2]))
        if click.confirm(click.style(u'Заменить в автоматическом режиме?', fg='yellow'), default=True):
            total[l_num] = new[l_num]
            styled(u'Заменено', 'yellow')
            replaced = True
    if replaced:
        with codecs.open(path, 'w', encoding) as f:
            f.write(u''.join(total))

def patch_fixer(base, new, to_replace, path, encoding):
    base_path = os.path.dirname(path)
    file_name = os.path.basename(path)
    patch_path = os.path.join(base_path, u'{}.patch'.format(file_name.rsplit('.', 1)[0]))
    if not to_replace:
        return
    with codecs.open(patch_path, 'w', 'utf-8') as f:
        f.write(u''.join(difflib.unified_diff(base, new, path, path)))
    with codecs.open(path, 'w', encoding) as f:
        f.write(u''.join(new))
    styled(u'Файл {} обновлен'.format(path), 'yellow')
    styled(u'Создан patch-файл {}'.format(patch_path), 'yellow')

@click.command()
@click.option('verbose', '-v', default=False, is_flag=True, help=u'Расширенный вывод')
@click.option('charset', '-c', help=u'Кодировка файлов. Если не указана, будет определена через libmagic')
@click.option('silent', '-s', default=False, is_flag=True,
              help=u'"Тихий" режим. Все замены будут произведены автоматически. '
                   u'В каталогах будут созданы path-файлы с аналогичными именами')
@click.option('inspections', '-i', type=click.Choice(PHPFixer.all_inspections), multiple=True,
              help=u'Включает только конкретные проверки из заданного списка')
@click.argument('path', type=click.Path(exists=True))
def parse(path, verbose, charset, silent, inspections):
    files = []
    if os.path.isdir(path):
        for directory, directories, dir_files in os.walk(path):
            for f in dir_files:
                full_path = os.path.join(directory, f)
                if is_php(full_path):
                    files.append(full_path)
    else:
        if not is_php(path):
            styled(u'Файл не является php файлом', 'red')
            return
        files = [path]
    if not files:
        styled(u'В директории не обнаружено файлов php', 'red')
        return
    fixer = PHPFixer(inspections=None if not inspections else inspections, charset=charset)
    for f in files:
        if verbose:
            styled(u'[{}] Проверяется файл: {}'.format(datetime.datetime.now().strftime('%H:%M:%S'), f), 'yellow')
        try:
            fixer.fix(f, fixer=patch_fixer if silent else click_fixer)
        except WontFix as e:
            styled(u'Не удалось проверить файл {}, ошибка: {}'.format(f, e), 'red')

    # print 'counts', fix_counts


if __name__ == '__main__':
    parse()
