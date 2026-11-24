"""Catalog-backed model selection. Display prices without altering API model IDs."""
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import ttk


def model_rates(model):
    rates = {}
    for source in ('pricing', 'pricing_skus'):
        prices = model.get(source)
        if not isinstance(prices, dict):
            continue
        for key, raw in prices.items():
            try:
                value = Decimal(str(raw))
            except InvalidOperation:
                continue
            if value.is_finite() and value >= 0:
                rates[key] = value
    return rates


def price_text(model):
    parts = []
    for key, rate in model_rates(model).items():
        if key in ('prompt', 'completion'):
            rate *= 1_000_000
            unit = ('input' if key == 'prompt' else 'output') + ' / 1M tokens'
        else:
            unit = key
        amount = format(rate, 'f')
        if '.' in amount:
            amount = amount.rstrip('0').rstrip('.')
        parts.append(f'${amount} {unit}')
    return '; '.join(parts) or 'ไม่ระบุราคา'


def filter_models(models, query='', provider='All', price='All', unit='', maximum=''):
    limit = None
    if maximum.strip():
        try:
            limit = Decimal(maximum)
        except InvalidOperation:
            return []
        if not limit.is_finite() or limit < 0 or not unit:
            return []
    result = []
    for model in models:
        identifier = model['id']
        if query.casefold() not in f'{identifier} {model.get("name", "")} {model.get("description", "")}'.casefold():
            continue
        if provider != 'All' and identifier.split('/')[0] != provider:
            continue
        rates = model_rates(model)
        category = 'Unknown' if not rates else 'Zero listed' if all(v == 0 for v in rates.values()) else 'Paid'
        if price != 'All' and price != category:
            continue
        if unit and unit not in rates:
            continue
        # Token limits use the same per-million scale as the displayed prices.
        if limit is not None and rates[unit] * (1_000_000 if unit in ('prompt', 'completion') else 1) > limit:
            continue
        result.append(model)
    return result


class ModelPicker(ttk.Frame):
    def __init__(self, parent, variable, models):
        super().__init__(parent)
        self.variable, self.models = variable, models
        self.columnconfigure(0, weight=1)
        self.display = tk.StringVar(self)
        self.combo = ttk.Combobox(self, textvariable=self.display, state='readonly',
                                  values=[self.label(m) for m in models])
        self.combo.grid(row=0, column=0, sticky='ew')
        self.button = ttk.Button(self, text='Filter…', command=self.open)
        self.button.grid(row=0, column=1, padx=(6, 0))
        self.combo.bind('<<ComboboxSelected>>', self.selected)
        self.trace = variable.trace_add('write', self.sync)
        self.sync()
        self.bind('<Destroy>', self.cleanup, add='+')

    @staticmethod
    def label(model):
        return model['id'] + ' | ' + price_text(model)

    def cleanup(self, event):
        if event.widget is self:
            self.variable.trace_remove('write', self.trace)

    def sync(self, *args):
        identifier = self.variable.get()
        model = next((m for m in self.models if m['id'] == identifier), {'id': identifier})
        self.display.set(self.label(model) if identifier else 'เลือกโมเดล / Filter…')

    def selected(self, event=None):
        index = self.combo.current()
        if index >= 0:
            self.variable.set(self.models[index]['id'])

    def open(self):
        popup = tk.Toplevel(self)
        popup.title('เลือกโมเดล • ราคา USD จาก catalog')
        popup.geometry('1000x600')
        popup.transient(self.winfo_toplevel())
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(2, weight=1)
        filters = ttk.Frame(popup, padding=12)
        filters.grid(row=0, column=0, sticky='ew')
        filters.columnconfigure(1, weight=1)
        query, provider, price, unit, maximum = [tk.StringVar(popup, value=v) for v in ('', 'All', 'All', '', '')]
        self.filter_vars = (query, provider, price, unit, maximum)
        specs = [('ค้นหาชื่อ / ID', query, None), ('Provider', provider, ['All'] + sorted({m['id'].split('/')[0] for m in self.models})),
                 ('ราคา', price, ['All', 'Zero listed', 'Paid', 'Unknown']),
                 ('หน่วยราคาจาก API', unit, [''] + sorted({k for m in self.models for k in model_rates(m)})),
                 ('สูงสุด USD (prompt/completion ต่อ 1M)', maximum, None)]
        for row, (label, variable, options) in enumerate(specs):
            ttk.Label(filters, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=3)
            widget = ttk.Entry(filters, textvariable=variable) if options is None else ttk.Combobox(filters, textvariable=variable, values=options, state='readonly')
            widget.grid(row=row, column=1, sticky='ew', pady=3)
        count = tk.StringVar(popup)
        ttk.Label(popup, textvariable=count, padding=(12, 0)).grid(row=1, sticky='w')
        table = ttk.Frame(popup, padding=12)
        table.grid(row=2, column=0, sticky='nsew')
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        tree = ttk.Treeview(table, columns=('id', 'price'), show='headings', selectmode='browse')
        tree.heading('id', text='Model ID')
        tree.heading('price', text='ราคา USD • หน่วยตาม API')
        tree.column('id', width=330)
        tree.column('price', width=600)
        tree.grid(row=0, column=0, sticky='nsew')
        for orient, command, row, col, sticky in [('vertical', tree.yview, 0, 1, 'ns'), ('horizontal', tree.xview, 1, 0, 'ew')]:
            bar = ttk.Scrollbar(table, orient=orient, command=command)
            bar.grid(row=row, column=col, sticky=sticky)
            tree.configure(**{('yscrollcommand' if orient == 'vertical' else 'xscrollcommand'): bar.set})
        def refresh(*args):
            tree.delete(*tree.get_children())
            for model in filter_models(self.models, *(v.get() for v in self.filter_vars)):
                tree.insert('', 'end', values=(model['id'], price_text(model)))
            count.set(f'{len(tree.get_children())} โมเดล • ราคา catalog อาจเปลี่ยนตามเงื่อนไข • ไม่ระบุราคา ≠ ฟรี')
        for variable in self.filter_vars:
            variable.trace_add('write', refresh)
        refresh()
        footer = ttk.Frame(popup, padding=12)
        footer.grid(row=3, column=0, sticky='ew')
        manual = tk.StringVar(popup, value=self.variable.get())
        ttk.Label(footer, text='Model ID (กรอกเองได้)').pack(side='left')
        ttk.Entry(footer, textvariable=manual).pack(side='left', fill='x', expand=True, padx=8)
        def choose(event=None):
            if manual.get().strip():
                self.variable.set(manual.get().strip())
                self.combo.event_generate('<<ComboboxSelected>>')
                popup.destroy()
        tree.bind('<<TreeviewSelect>>', lambda event: manual.set(tree.item(tree.selection()[0], 'values')[0]) if tree.selection() else None)
        tree.bind('<Double-1>', choose)
        tree.bind('<Return>', choose)
        ttk.Button(footer, text='เลือกโมเดล', command=choose).pack(side='right')
        popup.bind('<Escape>', lambda event: popup.destroy())
        popup.grab_set()
        filters.winfo_children()[1].focus_set()
