"""Native Tk/ttk calendar and time selector; no third-party dependency."""
import calendar
from datetime import datetime
import tkinter as tk
from tkinter import ttk


class DateTimePicker(ttk.Frame):
    def __init__(self, parent, variable, **kwargs):
        super().__init__(parent, **kwargs)
        self.variable = variable
        self.popup = None
        self.columnconfigure(0, weight=1)
        self.entry = ttk.Entry(self, textvariable=variable, state='readonly', font='TkDefaultFont')
        self.entry.grid(row=0, column=0, sticky='ew')
        self.button = ttk.Button(self, text='เลือกวัน / เวลา', command=self.open)
        self.button.grid(row=0, column=1, padx=(8, 0))

    def configure(self, cnf=None, **kwargs):
        state = kwargs.pop('state', None)
        result = super().configure(cnf, **kwargs) if cnf is not None or kwargs else None
        if state is not None:
            self.entry.configure(state='disabled' if state == 'disabled' else 'readonly')
            self.button.configure(state=state)
        return result

    config = configure

    def open(self):
        if self.popup and self.popup.winfo_exists():
            self.popup.lift()
            return
        try:
            self.selected = datetime.strptime(self.variable.get(), '%Y-%m-%d %H:%M')
        except ValueError:
            from zoneinfo import ZoneInfo
            self.selected = datetime.now(ZoneInfo('Asia/Bangkok')).replace(second=0, microsecond=0, tzinfo=None)
        self.year, self.month = self.selected.year, self.selected.month
        popup = self.popup = tk.Toplevel(self)
        popup.title('เลือกวันที่และเวลา / Date & Time')
        popup.transient(self.winfo_toplevel())
        popup.resizable(False, False)
        popup.protocol('WM_DELETE_WINDOW', self.close)
        popup.bind('<Escape>', lambda e: self.close())
        body = ttk.Frame(popup, padding=18)
        body.pack(fill='both', expand=True)
        navigation = ttk.Frame(body)
        navigation.pack(fill='x')
        ttk.Button(navigation, text='◀', command=lambda: self.move_month(-1)).pack(side='left')
        self.month_label = ttk.Label(navigation, anchor='center', width=23)
        self.month_label.pack(side='left', padx=10)
        ttk.Button(navigation, text='▶', command=lambda: self.move_month(1)).pack(side='right')
        self.calendar_frame = ttk.Frame(body)
        self.calendar_frame.pack(pady=12)
        time_frame = ttk.Frame(body)
        time_frame.pack(fill='x')
        ttk.Label(time_frame, text='เวลา / Time  ').pack(side='left')
        self.hour = tk.StringVar(value=f'{self.selected.hour:02}')
        self.minute = tk.StringVar(value=f'{self.selected.minute:02}')
        self.hour_spin = ttk.Spinbox(time_frame, from_=0, to=23, wrap=True, width=4, textvariable=self.hour, format='%02.0f')
        self.hour_spin.pack(side='left')
        ttk.Label(time_frame, text=' : ').pack(side='left')
        self.minute_spin = ttk.Spinbox(time_frame, from_=0, to=59, wrap=True, width=4, textvariable=self.minute, format='%02.0f')
        self.minute_spin.pack(side='left')
        self.hour.trace_add('write', self.time_changed)
        self.minute.trace_add('write', self.time_changed)
        self.selection = ttk.Label(body, text=self.variable.get(), anchor='center')
        self.selection.pack(fill='x', pady=10)
        ttk.Label(body, text='เลือกแล้วบันทึกอัตโนมัติ • เวลาเป็น timezone ของซีรีส์').pack(pady=5)
        ttk.Button(body, text='เสร็จ / Done', command=self.close).pack(fill='x')
        self.draw_calendar()
        popup.update_idletasks()
        x = min(self.winfo_rootx(), max(0, self.winfo_screenwidth() - popup.winfo_reqwidth() - 20))
        y = min(self.winfo_rooty() + self.winfo_height(), max(0, self.winfo_screenheight() - popup.winfo_reqheight() - 50))
        popup.geometry(f'+{max(0, x)}+{max(0, y)}')
        popup.grab_set()

    def move_month(self, delta):
        year, month = divmod(self.year * 12 + self.month - 1 + delta, 12)
        if 1 <= year <= 9999:
            self.year, self.month = year, month + 1
            self.draw_calendar()

    def draw_calendar(self):
        for widget in self.calendar_frame.winfo_children():
            widget.destroy()
        months = ('มกราคม', 'กุมภาพันธ์', 'มีนาคม', 'เมษายน', 'พฤษภาคม', 'มิถุนายน', 'กรกฎาคม', 'สิงหาคม', 'กันยายน', 'ตุลาคม', 'พฤศจิกายน', 'ธันวาคม')
        self.month_label.configure(text=f'{months[self.month-1]} / {self.month:02} • {self.year}')
        for column, name in enumerate(('จ.', 'อ.', 'พ.', 'พฤ.', 'ศ.', 'ส.', 'อา.')):
            ttk.Label(self.calendar_frame, text=name, anchor='center', width=4).grid(row=0, column=column)
        for row, week in enumerate(calendar.monthcalendar(self.year, self.month), 1):
            for column, day in enumerate(week):
                if day:
                    selected = (self.selected.year, self.selected.month, self.selected.day) == (self.year, self.month, day)
                    button = ttk.Button(self.calendar_frame, text=f'[{day}]' if selected else str(day), width=4, command=lambda d=day: self.select_day(d))
                    button.grid(row=row, column=column, padx=1, pady=1)

    def select_day(self, day):
        self.selected = self.selected.replace(year=self.year, month=self.month, day=day)
        self.commit()
        self.draw_calendar()

    def time_changed(self, *args):
        try:
            hour, minute = int(self.hour.get()), int(self.minute.get())
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                self.selected = self.selected.replace(hour=hour, minute=minute)
                self.commit()
        except ValueError:
            pass

    def commit(self):
        value = self.selected.strftime('%Y-%m-%d %H:%M')
        self.variable.set(value)
        self.selection.configure(text=value)

    def close(self):
        if self.popup and self.popup.winfo_exists():
            self.popup.grab_release()
            self.popup.destroy()
        self.popup = None
