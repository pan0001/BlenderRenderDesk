"""Shared visual components; render control is kept in core.py."""
import tkinter as tk
import customtkinter as ctk

BG = '#111115'
PANEL = '#1b1b22'
INK = '#eeeef4'
MUTED = '#9292a5'
ACCENT = '#ad9cff'
BORDER = '#2b2b36'
ctk.set_appearance_mode('dark')
ctk.set_widget_scaling(1.0)

def background(parent):
    try:
        color = parent.cget('fg_color')
        if isinstance(color, (tuple, list)):
            color = color[-1]
        return color if color != 'transparent' else background(parent.master)
    except Exception:
        return parent.cget('bg')

def label(parent, text, size=10, color=INK, bold=False, **kw):
    return tk.Label(parent, text=text, font=('Microsoft YaHei UI', size, 'bold' if bold else 'normal'),
                    bg=background(parent), fg=color, borderwidth=0, **kw)

def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=PANEL, border_color=BORDER,
                        border_width=1, corner_radius=14, **kw)

def button(parent, text='', command=None, style=None, **kw):
    primary = style == 'Accent.TButton'
    quiet = style == 'Quiet'
    return ctk.CTkButton(parent, text=text, command=command, corner_radius=8, height=36,
                        width=kw.pop('width', max(72, len(text)*13+24)),
                        fg_color='#8b76ed' if primary else ('transparent' if quiet else '#292933'),
                        hover_color='#9c88fc' if primary else '#373544',
                        text_color='#ffffff' if primary else '#d5d4e2',
                        text_color_disabled='#656372', font=('Microsoft YaHei UI', 12), **kw)

def entry(parent, textvariable=None, width=25, **kw):
    return ctk.CTkEntry(parent, textvariable=textvariable, width=width*8, height=36,
                       corner_radius=8, fg_color='#18181f', border_color='#353540',
                       border_width=1, text_color=INK, font=('Microsoft YaHei UI', 12), **kw)

def combo(parent, textvariable=None, values=None, width=20, state='readonly', **kw):
    return ctk.CTkComboBox(parent, variable=textvariable, values=values or [], width=width*9,
                         height=36, state=state, corner_radius=8, fg_color='#22222c',
                         border_color='#353540', button_color='#353540', button_hover_color='#494354',
                         dropdown_fg_color='#23232d', dropdown_hover_color='#3b344f',
                         text_color=INK, dropdown_text_color=INK,
                         font=('Microsoft YaHei UI', 12), dropdown_font=('Microsoft YaHei UI', 12), **kw)

def checkbox(parent, text='', variable=None, **kw):
    return ctk.CTkCheckBox(parent, text=text, variable=variable, checkbox_width=16, checkbox_height=16,
                         border_width=1, corner_radius=4, fg_color='#8b76ed', border_color='#626071',
                         hover_color='#514466', text_color=MUTED, font=('Microsoft YaHei UI', 11), **kw)

def dark_titlebar(win):
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), 4)
    except Exception:
        pass


class PageStack(tk.Frame):
    """A borderless page container, without native Notebook chrome."""
    def __init__(self, parent):
        super().__init__(parent, bg=BG, borderwidth=0, highlightthickness=0)
        self.current = None

    def add(self, page):
        pass

    def select(self, page=None):
        if page is None:
            return str(self.current)
        if self.current is not None:
            self.current.pack_forget()
        self.current = page
        page.pack(fill='both', expand=True)


class AutoScrollbar(ctk.CTkScrollbar):
    """Canvas-drawn dark scrollbar. Call grid() once; visibility follows viewport."""
    def __init__(self, parent, orient='vertical', command=None, **kw):
        super().__init__(parent, orientation=orient, command=command,
                         width=10 if orient == 'vertical' else 100,
                         height=10 if orient == 'horizontal' else 100,
                         fg_color=background(parent), button_color='#454151',
                         button_hover_color='#87779d', corner_radius=5,
                         border_spacing=2, minimum_pixel_length=24, **kw)
        self.visible = True

    def set(self, first, last):
        viewport = (float(first),float(last))
        if getattr(self,'_last_viewport',None) == viewport:
            return
        self._last_viewport = viewport
        super().set(first, last)
        wanted = float(first) > .00001 or float(last) < .99999
        if wanted != self.visible:
            self.visible = wanted
            if wanted:
                self.grid()
            else:
                self.grid_remove()


def progress_bar(parent):
    bar = ctk.CTkProgressBar(parent, height=5, corner_radius=3, border_width=0,
                           fg_color='#2d2936', progress_color='#a28bef')
    bar.set(0)
    return bar
