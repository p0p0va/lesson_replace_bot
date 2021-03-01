import uuid
import re
import sqlite3
from typing import Iterator, Callable
import datetime

import threading
import vk_api
from vk_api.longpoll import VkLongPoll, VkEventType
import remi.gui as gui
from contextlib import contextmanager
from remi import start, App

WIDTH = '120px'
MIN_WIDTH = '50px'
HEIGHT = '30px'
MARGIN = '10px'
ALL = '100%'


class DB:
    INIT_DB_QUERIES = (
        'CREATE TABLE IF NOT EXISTS lesson_replaces('
        ' date DATE, class_no INT, class_letter VARCHAR(1), lesson_no INT,'
        ' lesson VARCHAR(32), teacher VARCHAR(64));',
        'CREATE TABLE IF NOT EXISTS teachers(fio VARCHAR(64));',
        'CREATE TABLE IF NOT EXISTS lessons(lesson VARCHAR(32));',
    )

    def __init__(self, db_path: str = "app.db"):
        self.conn = sqlite3.Connection(db_path, check_same_thread=False)
        self.init_db()
        self.lock = threading.Lock()

    def init_db(self):
        cursor = self.conn.cursor()
        for query in self.INIT_DB_QUERIES:
            cursor.execute(query)
        self.conn.commit()

    @contextmanager
    def from_query(self, query, *args) -> Iterator[sqlite3.Cursor]:
        with self.lock:
            c = self.conn.cursor()
            try:
                c.execute(query, *args)
                self.conn.commit()
                yield c
            finally:
                c.close()

    def do_query(self, query, *args):
        with self.from_query(query, *args):
            pass


def get_db(store=[]):
    if not store:
        store.append(DB())
    return store[0]


class VkBot:
    def __init__(self, db: DB):
        token = open("vk.token").read().strip()
        self.session = vk_api.VkApi(token=token)
        self.api = self.session.get_api()
        self.longpool = VkLongPoll(self.session).listen()
        self.db = db
        self.class_rex = re.compile(r'(?P<no>\d+)(?P<letter>[А-Я])', re.U)
        self.family_rex = re.compile(r'(?P<family>[А-Я]+)', re.U)
        self.date_rex = re.compile(r'(\d{4}-\d{2}-\d{2})')

    @property
    def today(self) -> datetime.date:
        return datetime.datetime.now().date()

    @property
    def tomorrow(self) -> datetime.date:
        return (datetime.datetime.now() + datetime.timedelta(days=1)).date()

    def query_replaces(self, cond, params):
        params = params or (self.today, self.tomorrow)
        with self.db.from_query(
                f"SELECT date, class_no, class_letter, lesson_no, lesson, teacher"
                f" FROM lesson_replaces WHERE {cond};",
                params) as cursor:
            data = cursor.fetchall()
        return data

    def render_rows(self, rows) -> str:
        if not rows:
            return 'Я не нашел замен'
        result = 'Найдены замены:\n'
        for row in rows:
            result += f'Дата: {row[0]}, Класс: {str(row[1])+row[2]}, Урок: {row[3]}.{row[4]}, Учитель: {row[5]}\n'
        return result

    def process_command(self, text: str) -> str:
        err = None
        try:
            text = text.replace('Ё', 'Е')
            command, *params = text.upper().split(maxsplit=1)
            if command.startswith('ЗАМЕН') and params:
                param = params[0].strip()
                m_class = self.class_rex.match(param)
                m_family = self.family_rex.match(param)
                m_date = self.date_rex.match(param)
                tt = (self.today, self.tomorrow)
                if m_class:
                    no = m_class.group("no")
                    letter = m_class.group("letter")
                    cond = f"(date = ? OR date = ?) AND class_no = {no} AND class_letter = '{letter}'"
                    rows = self.query_replaces(cond, tt)
                    return self.render_rows(rows)
                elif m_family:
                    family = m_family.group("family")
                    cond = "(date = ? OR date = ?)"
                    rows = self.query_replaces(cond, tt)
                    rows = list(filter(lambda row: family in row[5].upper(), rows))
                    return self.render_rows(rows)
                elif m_date:
                    cond = "date = ?"
                    rows = self.query_replaces(cond, (param,))
                    return self.render_rows(rows)
        except Exception as e:
            err = str(e)

        unknown = 'Неизвестная команда или ошибка ввода.\nПравильно так: "замена 1Б", "замены 2021-02-28" или "замена Смирнов"\n'
        if err:
            unknown += f'\n\nError: [{err}]\n'
        return unknown

    def process_message(self):
        event = next(self.longpool)
        if event.type == VkEventType.MESSAGE_NEW and event.to_me and event.from_user:
            self.api.messages.send(
                user_id=event.user_id,
                message=self.process_command(event.text),
                random_id=uuid.uuid4().int,
            )


class ListWidget(gui.Container):
    def __init__(self, app: 'LessonReplaceBotApp', table: str, field: str, onchange: Callable):
        self.app = app
        self.table = table
        self.field = field
        self.onchange = onchange

        super().__init__(width=ALL, height='92%')
        hbox = gui.HBox(width=ALL, height=ALL)

        left_panel = gui.VBox(width="30%", height=ALL,
                              margin=MARGIN)
        left_panel.css_justify_content = "flex-start"
        right_panel = gui.VBox(width="70%", height=ALL,
                               margin=MARGIN)

        self.text = gui.TextInput('', width=ALL, height=HEIGHT, margin=MARGIN)
        self.add_btn = gui.Button('Добавить', width=ALL, height=HEIGHT, margin=MARGIN)
        self.add_btn.onclick.do(self.on_btn_add_click)
        self.del_btn = gui.Button('Удалить', width=ALL, height=HEIGHT, margin=MARGIN)
        self.del_btn.set_enabled(False)

        left_panel.append(self.text)
        left_panel.append(self.add_btn)
        left_panel.append(self.del_btn)

        self.list_view = gui.ListView(width=ALL, height=ALL, margin=MARGIN)
        self.refresh_list()
        self.list_view.onselection.do(self.list_view_on_selected)
        view_port = gui.Container(
            width=ALL, height=ALL,
            style={"overflow-y": "scroll"})
        view_port.append(self.list_view)
        right_panel.append(view_port)

        hbox.append(left_panel)
        hbox.append(right_panel)
        self.append(hbox)

    def list_view_on_selected(self, widget, selected_item_key):
        to_del = self.list_view.children[selected_item_key].get_text()

        def on_btn_del_click(widget):
            self.app.db.do_query(f"DELETE FROM {self.table} WHERE {self.field} = '{to_del}';")
            self.refresh_list()

        self.del_btn.set_enabled(True)
        self.del_btn.onclick.do(on_btn_del_click)

    def on_btn_add_click(self, widget):
        txt = self.text.text
        if txt:
            self.app.db.do_query(f"INSERT INTO {self.table}({self.field}) VALUES ('{txt}');")
            self.text.text = ''
            self.refresh_list()

    def refresh_list(self):
        self.onchange()
        self.list_view.empty()
        with self.app.db.from_query(f'SELECT {self.field} FROM {self.table};') as cursor:
            data = sorted(r[0] for r in cursor.fetchall())
            self.list_view.append([gui.ListItem(v, height=HEIGHT, style={'padding': '0'})
                                   for v in data])


class ReplacesManageWidget(gui.Container):
    def __init__(self, app: 'LessonReplaceBotApp'):
        self.app = app

        super().__init__(width=ALL, height='92%')
        style = {
            'justify-content': 'flex-start',
            'align-items': 'stretch',
            'background-color': 'transparent',
        }
        vbox = gui.VBox(width=ALL, height=ALL)
        vbox.style.update(style)
        vbox.style.update({
            'background-image': "url('https://sch1210sz.mskobr.ru/attach_files/logo/IMG_7037.png')",
            'background-repeat': 'no-repeat',
            'background-position': 'right top',
        })
        date_box = gui.HBox()
        date_box.style.update(style)
        now_str = datetime.datetime.now().strftime('%Y-%m-%d')
        self.date_picker = gui.Date(now_str, width=WIDTH, height=HEIGHT, margin=MARGIN)
        self.date_picker.onchange.do(lambda w, e: self.refresh_table())
        date_box.append(gui.Label("Дата:", height=HEIGHT, margin=MARGIN))
        date_box.append(self.date_picker)
        vbox.append(date_box)
        vbox.append(gui.Label("Новая замена:", height=HEIGHT, margin=MARGIN))
        repl_box = gui.HBox()
        repl_box.style.update(style)
        repl_box.append(gui.Label("Класс:", height=HEIGHT, margin=MARGIN))
        self.dd_class_no = gui.DropDown.new_from_list(
            [str(i) for i in range(1, 12)],
            height=HEIGHT, margin=MARGIN, width=MIN_WIDTH)
        repl_box.append(self.dd_class_no)
        rus_a = ord('А')
        self.dd_class_letter = gui.DropDown.new_from_list(
            [chr(i) for i in range(rus_a, rus_a + 32)],
            height=HEIGHT, margin=MARGIN, width=MIN_WIDTH)
        repl_box.append(self.dd_class_letter)
        repl_box.append(gui.Label("Урок:", height=HEIGHT, margin=MARGIN))
        self.dd_lesson_no = gui.DropDown.new_from_list(
            [str(i) for i in range(1, 11)],
            height=HEIGHT, margin=MARGIN, width=MIN_WIDTH)
        repl_box.append(self.dd_lesson_no)

        repl_box.append(gui.Label("Предмет:", height=HEIGHT, margin=MARGIN))
        self.dd_lesson = gui.DropDown(height=HEIGHT, margin=MARGIN, width=WIDTH)
        repl_box.append(self.dd_lesson)
        self.refresh_lesson_dd()

        repl_box.append(gui.Label("Учитель:", height=HEIGHT, margin=MARGIN))
        self.dd_teacher = gui.DropDown(height=HEIGHT, margin=MARGIN, width=WIDTH)
        repl_box.append(self.dd_teacher)
        self.refresh_teacher_dd()

        self.add_btn = gui.Button("Добавить", height=HEIGHT, width=WIDTH, margin=MARGIN)
        self.add_btn.onclick.do(self.on_add_btn_click)
        repl_box.append(self.add_btn)

        vbox.append(repl_box)
        vbox.append(gui.Label("Запланированные замены:", height=HEIGHT, margin=MARGIN))
        self.table = gui.Table(width='95%', margin=MARGIN)
        view_port = gui.Container(
            width=ALL, height=ALL,
            style={"overflow-y": "scroll"})
        view_port.append(self.table)
        vbox.append(view_port)
        self.refresh_table()
        self.append(vbox)

    def refresh_table(self):
        td_style = {'padding': '5px'}
        self.table.empty()
        date = self.date_picker.get_value()
        header = ('Класс', 'Урок', 'Предмет', 'Учитель', 'Удаление')
        tr = gui.TableRow()
        for col in header:
            tr.append(gui.TableTitle(col, style=td_style))
        self.table.append(tr)
        with self.app.db.from_query(
                "SELECT class_no, class_letter, lesson_no, lesson, teacher"
                " FROM lesson_replaces WHERE date = ?;", (date,)) as cursor:
            rows = cursor.fetchall()
            for row in rows:
                tr = gui.TableRow()
                tr.append(str(row[0])+row[1])
                tr.append(str(row[2]))
                tr.append(row[3])
                tr.append(row[4])
                del_cell = gui.TableItem('[X] Удалить', style=td_style)

                def on_del_click(widget, to_drop):
                    self.app.db.do_query('DELETE FROM lesson_replaces WHERE '
                                          ' date = ? AND class_no = ? AND class_letter = ? AND'
                                          ' lesson_no = ? AND lesson = ? AND teacher = ?;',
                                          to_drop)
                    self.refresh_table()

                tr.append(del_cell)
                del_cell.onclick.do(on_del_click, (date,) + row)

                self.table.append(tr)

    def refresh_lesson_dd(self):
        with self.app.db.from_query("SELECT DISTINCT(lesson) FROM lessons;") as cursor:
            lessons = sorted(r[0] for r in cursor.fetchall())
        self.dd_lesson.empty()
        for lesson in lessons:
            self.dd_lesson.append(lesson)

    def on_add_btn_click(self, widget):
        row = (self.date_picker.get_value(),
               int(self.dd_class_no.get_value()),
               self.dd_class_letter.get_value(),
               int(self.dd_lesson_no.get_value()),
               self.dd_lesson.get_value(),
               self.dd_teacher.get_value(),
               )
        self.app.db.do_query('INSERT INTO lesson_replaces(date, class_no, class_letter, lesson_no, lesson, teacher)'
                             ' VALUES (?, ?, ?, ?, ?, ?);', row)
        self.refresh_table()

    def refresh_teacher_dd(self):
        with self.app.db.from_query("SELECT DISTINCT(fio) FROM teachers;") as cursor:
            teachers = sorted(r[0] for r in cursor.fetchall())
        self.dd_teacher.empty()
        for teacher in teachers:
            self.dd_teacher.append(teacher)


class LessonReplaceBotApp(App):
    def __init__(self, *args):
        super(LessonReplaceBotApp, self).__init__(*args)

    @property
    def db(self):
        return get_db()

    def main(self):
        main_container = gui.VBox(
            width="80%",
            height=ALL,
            style={'margin': '0px auto',
                   'position': 'relative'})

        tb = gui.TabBox(width=ALL, height=ALL)
        cw = ReplacesManageWidget(self)
        tb.add_tab(cw, 'Замены')
        tb.add_tab(ListWidget(self, table='lessons', field='lesson',
                              onchange=lambda: cw.refresh_lesson_dd()), 'Предметы')
        tb.add_tab(ListWidget(self, table='teachers', field='fio',
                              onchange=lambda: cw.refresh_teacher_dd()), 'Учителя')

        main_container.add_child("tab_box", tb)
        return main_container


def main():
    vk_bot = VkBot(get_db())

    def bot_loop():
        while True:
            vk_bot.process_message()

    thread = threading.Thread(target=bot_loop)
    thread.daemon = True

    thread.start()
    start(LessonReplaceBotApp, debug=True, address='0.0.0.0', port=5005)


if __name__ == "__main__":
    main()
