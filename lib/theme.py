"""Shared quiet, light theme for the main window and previews."""

from tkinter import ttk


BG = '#F4F7FB'
CARD = '#FFFFFF'
TEXT = '#24354B'
MUTED = '#6C7E93'
BORDER = '#DFE7F0'
ACCENT = '#437BD0'
TINT = '#EAF2FF'
TEAL = '#317F7B'
FONT = 'Microsoft YaHei UI'


def configure_theme(root):
    root.configure(background=BG)
    root.option_add('*Menu.Font', (FONT, 10))
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=(FONT, 10), background=BG, foreground=TEXT)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabel', background=BG, foreground=TEXT)
    style.configure('Card.TLabel', background=CARD)
    style.configure('Muted.TLabel', foreground=MUTED, font=(FONT, 9))
    style.configure('CardMuted.TLabel', background=CARD, foreground=MUTED, font=(FONT, 9))
    style.configure('Title.TLabel', font=(FONT, 21, 'bold'))
    style.configure('Heading.TLabel', font=(FONT, 12, 'bold'), background=CARD)
    style.configure('Metric.TLabel', font=(FONT, 20, 'bold'), background=CARD)
    style.configure('TButton', padding=(12, 6), background=CARD, bordercolor=BORDER,
                    relief='flat', focusthickness=1, focuscolor=ACCENT)
    style.map('TButton', background=[('active', TINT)], foreground=[('disabled', '#A3AEBB')])
    style.configure('Accent.TButton', background=ACCENT, foreground='white', bordercolor=ACCENT)
    style.map('Accent.TButton', background=[('active', '#346AB9'), ('disabled', '#BACDE8')],
              foreground=[('disabled', 'white')])
    style.configure('Recycle.TButton', background='#FBEDED', foreground='#AC4E51', bordercolor='#FBEDED')
    style.map('Recycle.TButton', background=[('active', '#F5DEDF')])
    style.configure('TEntry', fieldbackground=CARD, bordercolor=BORDER, padding=7)
    style.configure('TCombobox', fieldbackground=CARD, bordercolor=BORDER, padding=5)
    style.map('TCombobox', fieldbackground=[('readonly', CARD)])
    style.configure('TSpinbox', fieldbackground=CARD, bordercolor=BORDER, padding=5)
    style.configure('TCheckbutton', background=BG, padding=3)
    style.configure('Card.TCheckbutton', background=CARD)
    style.map('TCheckbutton', background=[('active', TINT)])
    style.configure('Treeview', background=CARD, fieldbackground=CARD, rowheight=34,
                    borderwidth=0, foreground=TEXT)
    style.configure('Treeview.Heading', background='#EDF3FA', foreground=MUTED,
                    font=(FONT, 10, 'bold'), padding=(12, 7), relief='flat')
    style.map('Treeview', background=[('selected', TINT)], foreground=[('selected', TEXT)])
    style.configure('TNotebook', background=BG, borderwidth=0)
    style.configure('TNotebook.Tab', padding=(18, 7), background=BG, foreground=MUTED)
    style.map('TNotebook.Tab', background=[('selected', CARD)], foreground=[('selected', ACCENT)])
    style.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor='#E6EEF8',
                    bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT, thickness=4)
    style.configure('TScrollbar', background='#E1E8F1', troughcolor=CARD, bordercolor=CARD,
                    arrowcolor=MUTED, lightcolor=CARD, darkcolor=CARD)
    return style
