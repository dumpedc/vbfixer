# -*- coding: utf-8 -*-

import click
import codecs
import collections
import datetime
import difflib
import magic
import os
import peewee
from pygments.lexers.php import PhpLexer
from pygments.token import Token, is_token_subtype


class WontFix(Exception):
    pass


db = peewee.Proxy()


class Plugin(peewee.Model):
    active = peewee.IntegerField()
    devkey = peewee.CharField(max_length=25)
    executionorder = peewee.IntegerField()
    hookname = peewee.CharField(max_length=250)
    phpcode = peewee.TextField(null=True)
    pluginid = peewee.PrimaryKeyField()
    product = peewee.CharField(max_length=25)
    title = peewee.CharField(max_length=250)

    class Meta:
        db_table = 'plugin'
        database = db

# fix_counts = {'alarms': 0, 'refs': 0}


class Fixer(object):
    VBULLETIN_GLOBALS = ('$vbulletin', '$db',)
    all_inspections = (
        'assign_by_reference',  # Исправляет deprecation warning
    )

    def __init__(self, inspections=None, charset=None):
        self.charset = charset
        self.inspections = (
            self.all_inspections if inspections is None else filter(lambda x: x in self.all_inspections, inspections)
        )

    def get_inspections(self):
        return [
            {
                'process': getattr(self, 'process_{}'.format(inspection)),
                'context_getter': getattr(self, 'get_context_{}'.format(inspection), self.get_context)
            } for inspection in self.inspections
        ]

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


class PHPFixer(Fixer):
    def detect_encoding(self, path):
        with open(path, 'rb') as f:
            blob = f.read()
            m = magic.Magic(mime_encoding=True)
            return m.from_buffer(blob)

    def process_php_file_line_by_line(self, path, func):
        try:
            encoding = self.charset or self.detect_encoding(path)
            with codecs.open(path, 'r', encoding) as f:
                for l_num, line in enumerate(f.readlines()):
                    func(l_num, line)
        except (LookupError, magic.MagicException,):
            raise WontFix('Encoding problem')
        else:
            return encoding

    def read_config(self, path):
        inline_lexer = PhpLexer(startinline=True)

        infinity_dict = lambda: collections.defaultdict(infinity_dict)
        config = infinity_dict()

        def process_config(l_num, line):
            context = {'is_config': False, 'config_group': [], 'group_ended': False, 'config_value': []}
            for token, text in inline_lexer.get_tokens(line):
                is_comment = is_token_subtype(token, Token.Comment)
                is_string = is_token_subtype(token, Token.Literal.String)
                if is_comment:
                    return
                if context['is_config'] and token is Token.Punctuation and ';' in text:
                    context['is_config'] = False
                if context['is_config'] and not context['group_ended'] and is_string:
                    context['config_group'].append(text[1:-1])
                if context['is_config'] and context['group_ended'] and token is not Token.Text:
                    context['config_value'].append((token, text))
                if context['is_config'] and token is Token.Operator and text == '=':
                    context['group_ended'] = True
                if token is Token.Name.Variable and text == '$config':
                    context['is_config'] = True
                # print token, (text,)
            if context['config_group']:
                value = []
                for token, val in context['config_value']:
                    if token is Token.Literal.Number.Integer:
                        value.append(int(val))
                    elif token is Token.Literal.Number.Float:
                        value.append(float(val))
                    elif is_token_subtype(token, Token.Literal.String):
                        value.append(val[1:-1])
                    elif token is Token.Keyword:
                        value.append({'true': True, 'false': False, 'null': None}.get(val))
                    # else:
                    #     print token, val
                group = config
                for g in context['config_group'][:-1]:
                    group = group[g]
                group[context['config_group'][-1]] = value
                # print u'is_line config', context

        self.process_php_file_line_by_line(path, func=process_config)
        return config

    def fix(self, path, fixer=lambda base, new, to_replace, path, encoding: None):
        inline_lexer = PhpLexer(startinline=True)
        inspections = self.get_inspections()
        base_lines = []
        fixed_lines = []
        to_replace = []

        def process_inspections(l_num, line):
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

        encoding = self.process_php_file_line_by_line(path, func=process_inspections)

        if to_replace:
            fixer(base_lines, fixed_lines, to_replace, path, encoding)
        # print base_lines
        # print fixed_lines


class MySQLFixer(Fixer):
    def __init__(self, inspections=None, charset=None, config=None):
        self.config = config
        self.initialize_db()
        super(MySQLFixer, self).__init__(inspections=inspections, charset=charset)

    def initialize_db(self):
        database = self.config['Database']['dbname'][0] if self.config['Database']['dbname'] else None
        user = self.config['MasterServer']['username'][0] if self.config['MasterServer']['username'] else None
        host = self.config['MasterServer']['servername'][0] if self.config['MasterServer']['servername'] else None
        password = self.config['MasterServer']['password'][0] if self.config['MasterServer']['password'] else ''
        port = self.config['MasterServer']['port'][0] if self.config['MasterServer']['port'] else None
        charset = self.config['Mysqli']['charset'][0] if self.config['Mysqli']['charset'] else None
        prefix = self.config['Database']['tableprefix'][0] if self.config['Database']['tableprefix'] else ''
        if database is None:
            raise WontFix('Unknown database')
        if user is None:
            raise WontFix('No mysql username provided')
        if password is None:
            raise WontFix('No mysql password provided')
        # Если кто смотрит - в реальных проектах так делать нельзя.
        # Нельзя просто так тасовать глобальное подключение к базе данных неявно из какого-то левого класса
        db.initialize(peewee.MySQLDatabase(
            database=database, host=host or 'localhost', port=port or 3606, user=user, passwd=password, charset=charset
        ))
        try:
            db.connect()
        except Exception as e:
            raise WontFix('Unable to connect to MySQL: {}'.format(e))
        # Ну а это вообще грязный хак чистой воды
        Plugin._meta.db_table = u'{}plugin'.format(prefix)

    def process_php_code_line_by_line(self, code, func):
        for l_num, line in enumerate(code.splitlines()):
            func(l_num, u'{}\n'.format(line))

    def fix(self, fixer=lambda base, new, to_replace, obj: None):
        inline_lexer = PhpLexer(startinline=True)
        inspections = self.get_inspections()
        for plugin in Plugin.select().order_by(Plugin.pluginid):
            base_lines = []
            fixed_lines = []
            to_replace = []

            def process_inspections(l_num, line):
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

            self.process_php_code_line_by_line(plugin.phpcode, func=process_inspections)

            if to_replace:
                fixer(base_lines, fixed_lines, to_replace, plugin)


# В общем случае это далеко не всегда определяется исключительно расширением файла, но нас не интересует
is_php = lambda path: os.path.basename(path).rsplit('.', 1)[-1] == 'php'
# Шоткат до стилизованного вывода click
styled = lambda text, color: click.echo(click.style(text, fg=color))
# Шоткат получения времени для вывода в терминал
dt_now = lambda: datetime.datetime.now().strftime('%H:%M:%S')

def click_print_diff(path, base, new, l_num):
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

def click_fixer(base, new, to_replace, path, encoding):
    total = list(base)
    replaced = False
    for l_num in to_replace:
        click_print_diff(path, base, new, l_num)
        if click.confirm(click.style(u'Заменить в автоматическом режиме?', fg='yellow'), default=True):
            total[l_num] = new[l_num]
            styled(u'Заменено', 'yellow')
            replaced = True
    if replaced:
        with codecs.open(path, 'w', encoding) as f:
            f.write(u''.join(total))

def click_mysql_fixer(base, new, to_replace, obj):
    total = list(base)
    replaced = False
    for l_num in to_replace:
        click_print_diff(u'[{}] {}'.format(obj.hookname, obj.title), base, new, l_num)
        if click.confirm(click.style(u'Заменить в автоматическом режиме', fg='yellow'), default=True):
            total[l_num] = new[l_num]
            styled(u'Заменено', 'yellow')
            replaced = True
    if replaced:
        obj.phpcode = u''.join(total)
        obj.save(only=[Plugin.phpcode])

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
@click.option('no_mysql', '--no-mysql', is_flag=True, default=False, help=u'Не проверять модули в базе данных')
@click.argument('path', type=click.Path(exists=True))
def parse(path, verbose, charset, silent, no_mysql, inspections):
    files = []
    configs = []
    if os.path.isdir(path):
        for directory, directories, dir_files in os.walk(path):
            for f in dir_files:
                full_path = os.path.join(directory, f)
                if is_php(full_path):
                    files.append(full_path)
                    if directory == 'includes' and f == 'config.php':
                        configs.append(full_path)
    else:
        if not is_php(path):
            styled(u'Файл не является php файлом', 'red')
            return
        files = [path]
        if os.path.basename(path) == 'config.php':
            configs = [path]
    if not files:
        styled(u'В директории не обнаружено файлов php', 'red')
        return
    fixer = PHPFixer(inspections=None if not inspections else inspections, charset=charset)
    for f in files:
        if verbose:
            styled(u'[{}] Проверяется файл: {}'.format(dt_now(), f), 'yellow')
        try:
            fixer.fix(f, fixer=patch_fixer if silent else click_fixer)
        except WontFix as e:
            styled(u'Не удалось проверить файл {}, ошибка: {}'.format(f, e), 'red')
    if no_mysql:
        return
    for c in configs:
        if verbose:
            styled(u'[{}] Считывается конфиг: {}'.format(dt_now(), c), 'yellow')
        try:
            config = fixer.read_config(c)
        except WontFix as e:
            styled(u'Не удалось прочитать конфиг {}, ошибка: {}'.format(c, e), 'red')
            continue
        try:
            mysql_fixer = MySQLFixer(inspections=None if not inspections else inspections, config=config)
        except WontFix as e:
            styled(u'Не удалось установить связь с MySQL, ошибка: {}'.format(e), 'red')
            continue
        try:
            mysql_fixer.fix(fixer=click_mysql_fixer)
        except WontFix as e:
            styled(u'Не удалось обновить модули в базе данных, ошибка: {}'.format(e), 'red')
            continue

    # print 'counts', fix_counts


if __name__ == '__main__':
    parse()
