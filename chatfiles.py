"""ChatFiles: local conversation workspace inventory. Standard library only."""
import os, sys, json, sqlite3, threading, queue, ctypes, csv
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

VERSION='0.2.8'
WORKSPACE_NOTICE = 'AI 对话关联的工作目录可能包含源码、报告、实验结果和原始素材。请先核对文件用途，再清理确认不再需要的内容。'
def normal(p): return os.path.normcase(os.path.abspath(str(p).removeprefix('\\\\?\\')))
def inside(p, root):
    try: return os.path.commonpath([normal(p),normal(root)])==normal(root)
    except ValueError: return False
def human(n):
    if n is None:return '尚未统计'
    for unit in ['B','KB','MB','GB','TB']:
        if n<1024:return f'{n:.1f} {unit}'
        n/=1024
    return f'{n:.1f} PB'
def databases(folder):
    folder = os.fspath(folder).strip()
    if not folder:
        return []
    p=Path(os.path.expandvars(folder.strip().strip('"'))).expanduser()
    if p.is_file() and p.suffix=='.sqlite':return [p]
    choices=[p,p/'.codex',p/'Codex',p/'LocalState',p/'LocalCache'/'Roaming'/'Codex']
    found=[]
    for c in choices:
        if c.is_dir():found.extend(c.glob('state_*.sqlite'))
    return sorted(set(found),key=lambda p:p.stat().st_mtime,reverse=True)
def detect():
    candidates=[os.environ.get('CODEX_HOME',''),str(Path.home()/'.codex')]
    local=Path(os.environ.get('LOCALAPPDATA',Path.home()/'AppData'/'Local'))
    candidates.extend([str(local/'OpenAI'),str(local/'Codex')])
    packages=local/'Packages'
    if packages.exists():candidates.extend(str(p) for p in packages.glob('OpenAI.*'))
    for p in candidates:
        if p and databases(p):return str(databases(p)[0])
    return ''
def read_threads(db):
    names={}
    try:
        with (Path(db).parent/'session_index.jsonl').open(encoding='utf-8-sig') as index:
            for line in index:
                try:
                    entry=json.loads(line)
                    if isinstance(entry,dict) and isinstance(entry.get('thread_name'),str):
                        names[entry.get('id')]=entry['thread_name'].strip()
                except ValueError:
                    continue
    except OSError:
        pass
    con=sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
    try:
        columns={r[1] for r in con.execute('pragma table_info(threads)')}
        if not {'id','cwd','title'}<=columns:raise ValueError('此数据库没有可识别的本地对话—工作目录映射。')
        updated='updated_at' if 'updated_at' in columns else '0'
        archived='archived' if 'archived' in columns else '0'
        result=[]
        source='source' if 'source' in columns else "''"
        name='name' if 'name' in columns else "''"
        for ident,title,cwd,stamp,arc,origin,custom in con.execute(f'select id,title,cwd,{updated},{archived},{source},{name} from threads order by {updated} desc'):
            if not cwd:continue
            if 'subagent' in (origin or '').lower():continue
            label=(custom or '').strip() or names.get(ident) or (title or '').strip() or '未命名对话'
            result.append(dict(id=ident,title=label,path=normal(cwd),updated=stamp,archived=bool(arc)))
        return result
    finally:con.close()
def conflicts(path,threads):
    return [t for t in threads if inside(t['path'],path) or inside(path,t['path'])]
def scan(folder):
    result=[];errors=[];stack=[normal(folder)]
    while stack:
        p=stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_symlink() or (hasattr(e,'is_junction') and e.is_junction()):continue
                        if e.is_dir(follow_symlinks=False):stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            s=e.stat(follow_symlinks=False);result.append((e.path,s.st_size,s.st_mtime))
                    except OSError as ex:errors.append(str(ex))
        except OSError as ex:errors.append(str(ex))
    result.sort(key=lambda r:r[1],reverse=True)
    return result,errors
def protect(path,workspace,data_home,*,whole_directory=False):
    p=Path(normal(path));w=Path(normal(workspace));home=Path.home()
    if not inside(p,w):raise ValueError('目标超出当前对话工作目录。')
    if not p.exists():raise ValueError('目标路径已不存在。可能已移动或删除，请刷新大小。')
    if whole_directory:
        protected_roots = {normal(home), *(normal(home/name) for name in ('Desktop','Documents','Downloads','AppData','Pictures','Videos','Music'))}
        program = sys.executable if getattr(sys,'frozen',False) else __file__
        if inside(program,p):
            raise ValueError('软件保护：ChatFiles 正从这个目录或其子目录运行。请关闭软件，将它移到目标目录之外，再启动重试。')
        if p != w or p == p.parent or not p.is_dir() or normal(p) in protected_roots:
            raise ValueError('软件保护：此目标是磁盘根目录、用户常用目录或无效工作目录。请选择具体文件清理。')
    elif p==w:
        raise ValueError('请选择具体文件。')
    sensitive=[Path(os.environ.get('WINDIR',r'C:\Windows')),Path(os.environ.get('ProgramFiles',r'C:\Program Files')),Path(os.environ.get('ProgramFiles(x86)',r'C:\Program Files (x86)')),home/'.codex',Path(data_home)]
    if any(inside(p,s) or inside(s,p) for s in sensitive):raise ValueError('软件保护：目标涉及系统、程序或对话索引目录。请保留这些目录，仅清理已确认的项目文件。')
    if any(x.lower() in {'.git','.svn'} for x in p.parts):raise ValueError('版本库元数据需要保留。')
    for ancestor in [p,*p.parents]:
        if ancestor.is_symlink() or (hasattr(ancestor,'is_junction') and ancestor.is_junction()):raise ValueError('不处理符号链接或目录联接目标。')
    return str(p)
def recycle(paths):
    """Windows shell recycle operation; no permanent-delete fallback."""
    if sys.platform!='win32':raise RuntimeError('回收站操作仅支持 Windows。')
    from ctypes import wintypes
    class OP(ctypes.Structure):
        _fields_=[('hwnd',wintypes.HWND),('wFunc',wintypes.UINT),('pFrom',wintypes.LPCWSTR),('pTo',wintypes.LPCWSTR),('fFlags',wintypes.WORD),('fAnyOperationsAborted',wintypes.BOOL),('hNameMappings',ctypes.c_void_p),('lpszProgressTitle',wintypes.LPCWSTR)]
    # No FOF_NOCONFIRMATION: retain Windows confirmation and cancellation UI.
    op=OP();op.wFunc=3;op.pFrom='\0'.join(paths)+'\0\0';op.fFlags=0x40|0x4000
    call=ctypes.windll.shell32.SHFileOperationW;call.argtypes=[ctypes.POINTER(OP)];call.restype=ctypes.c_int
    rc=call(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted:
        raise RuntimeError(recycle_failure(rc,bool(op.fAnyOperationsAborted)))
    for path in paths:
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise RuntimeError(f'Windows 已返回结果，但无法核对目标路径。{error}') from error
        raise RuntimeError('Windows 返回完成，但原路径仍存在。目标可能未移入回收站，也可能已被其他程序重新创建。')

def recycle_failure(code, aborted):
    code &= 0xFFFFFFFF
    if code in {0x20,0x21,0x80070020,0x80070021}:
        reason = 'Windows 报告目标可能被占用。请关闭正在访问它的编辑器、仿真工具或终端，再重试。'
    elif code in {5,0x78,0x80070005}:
        reason = 'Windows 拒绝访问。请检查当前账户对目标和上级目录的权限。'
    elif code in {0x79,0x81,0x800700CE}:
        reason = 'Windows 回收站操作遇到路径长度限制。可打开目录，从较深的子文件夹分批处理。'
    elif code in {2,3,0x80070002,0x80070003}:
        reason = 'Windows 找不到目标路径。请检查文件是否已移动，并刷新列表。'
    elif code in {0x75,1223,0x800704C7}:
        reason = 'Windows 报告操作已取消。若主动取消，可重新发起。'
    elif aborted and not code:
        reason = 'Windows 报告操作中止，但未提供具体原因。请查看系统提示并核对目录。'
    else:
        reason = 'Windows 未完成回收站操作，具体原因未确认。请核对目标是否存在，以及占用、权限和回收站设置。'
    return f'{reason}\n系统代码 0x{code:X}'

FILE_TYPES = {
    '文档': '.txt .md .pdf .doc .docx .ppt .pptx .xls .xlsx .rtf',
    '图片': '.png .jpg .jpeg .gif .webp .svg .bmp .tif .ico',
    '视频': '.mp4 .mov .avi .mkv .webm .wmv .m4v',
    '音频': '.mp3 .wav .aac .flac .m4a .ogg .opus',
    '压缩包': '.zip .rar .7z .tar .gz .bz2 .xz',
    '安装包': '.msi .msix .appx .appxbundle .msixbundle',
    '代码与脚本': '.py .js .ts .c .cpp .h .sv .v .vhd .ps1 .sh .tcl .html .css',
    '数据与数据库': '.csv .tsv .json .jsonl .sqlite .db .duckdb .parquet',
    '日志': '.log .etl .jou',
    '工程与仿真': '.wdb .wlf .vcd .fst .fsdb .vpd .xpr .qpf .qsf .bit .dcp',
    '程序': '.exe .com .bat .cmd',
}
TYPE_BY_SUFFIX = {ext: label for label, extensions in FILE_TYPES.items() for ext in extensions.split()}

def file_type(path):
    return TYPE_BY_SUFFIX.get(Path(path).suffix.lower(), '其他')

def configure_windows_dpi():
    if sys.platform == 'win32':
        # Tk 8.6 uses system DPI. Call before creating any window.
        ctypes.windll.user32.SetProcessDPIAware()


def desktop_size(root):
    if sys.platform == 'win32':
        from ctypes.wintypes import RECT
        area = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
            return area.right - area.left, area.bottom - area.top
    return root.winfo_screenwidth(), root.winfo_screenheight()


class App:
    def __init__(self,root):
        self.root = root
        root.title('ChatFiles · 对话文件管理')
        self.scale = float(root.tk.call('tk','scaling')) / (96 / 72)
        screen_w, screen_h = desktop_size(root)
        width = min(self.px(1260), max(440, screen_w - self.px(32)))
        height = min(self.px(790), max(360, screen_h - self.px(64)))
        root.geometry(f'{width}x{height}')
        root.minsize(min(self.px(790),width),min(self.px(500),height))
        root.configure(background='#f1f5fa')
        self.events=queue.Queue();self.threads=[];self.cache={};self.sizes={};self.active=None;self.files=[];self.busy=False;self.thread_keys={};self.file_keys={};self.path_counts={};self.path_related={}
        self.loaded_db = None
        self.db=tk.StringVar(value=detect());self.query=tk.StringVar();self.status=tk.StringVar(value='准备就绪。只读扫描；所有清理需单独确认。')
        self.file_query = tk.StringVar()
        self.category = tk.StringVar(value='全部类型')
        self.min_size = tk.StringVar(value='全部大小')
        self.selection_info = tk.StringVar(value='未选择文件')
        self.filter_timer = None
        self.sort_descending = False
        self.byte_totals = {}
        self.total_info = tk.StringVar(value='对话大小合计　—')
        self.total_detail = tk.StringVar(value='共享目录和已计入的子目录只统计一次。')
        self.configure_theme()
        header = ttk.Frame(root,style='Surface.TFrame',padding=self.pads(18,5))
        header.pack(fill='x')
        ttk.Label(header,text='ChatFiles',style='Brand.TLabel').pack(side='left')
        ttk.Label(header,text='对话文件管理',style='SurfaceMuted.TLabel').pack(side='left',padx=self.px(12))
        ttk.Button(header,text='对话目录设置',command=self.settings).pack(side='right')
        ttk.Label(header,text='v'+VERSION,style='SurfaceMuted.TLabel').pack(side='right',padx=self.px(12))

        ttk.Separator(root).pack(fill='x')
        panes = ttk.Panedwindow(root,orient='horizontal')
        left = ttk.Frame(panes,style='Sidebar.TFrame',padding=self.pads(12,10))
        right = ttk.Frame(panes,style='Surface.TFrame',padding=self.pads(15,10))
        panes.add(left,weight=1)
        panes.add(right,weight=3)
        self.sidebar_button = ttk.Button(header,text='隐藏侧栏',command=self.toggle_sidebar)
        self.sidebar_button.pack(side='right',padx=self.px(4))
        self.panes = panes
        self.sidebar = left
        self.sidebar_width = None

        summary = ttk.Frame(left,style='Accent.TFrame',padding=self.pads(9,6))
        summary.pack(side='bottom',fill='x',pady=self.pads(8,0))
        ttk.Label(summary,textvariable=self.total_info,style='Metric.TLabel').pack(fill='x')
        summary_detail = ttk.Label(summary,textvariable=self.total_detail,style='Accent.TLabel')
        summary_detail.pack(fill='x')
        self.wrap_to(summary_detail,4)

        ttk.Label(left,text='对话',style='SidebarSection.TLabel').pack(anchor='w',pady=self.pads(0,5))
        toolbar = ttk.Frame(left,style='Sidebar.TFrame')
        toolbar.pack(fill='x',pady=self.pads(0,8))
        self.scan_all_btn = ttk.Button(toolbar,text='刷新大小',command=self.scan_all)
        self.scan_all_btn.pack(side='right',padx=self.pads(5,0))
        search = ttk.Frame(toolbar,style='Sidebar.TFrame')
        search.pack(side='left',fill='x',expand=True)
        ttk.Label(search,text='搜索对话',style='SidebarMuted.TLabel').pack(anchor='w')
        ttk.Entry(search,textvariable=self.query,width=8).pack(fill='x')
        self.query.trace_add('write',lambda *_:self.render_threads())

        self.tree = self.table(left,[('title','对话名称',175),('size','大小 ↕',76),('shared','关联',85)],'browse')
        self.tree.heading('size',command=self.sort_by_size)
        self.tree.bind('<<TreeviewSelect>>',self.selected)

        ttk.Label(right,text='工作目录',style='Section.TLabel').pack(anchor='w')
        self.info = tk.StringVar(value='选择一条对话查看文件。')
        info = ttk.Label(right,textvariable=self.info,style='SurfaceMuted.TLabel',padding=self.pads(0,3,0,5))
        info.pack(fill='x')
        self.wrap_to(info)
        notice = ttk.Label(right,text='工作目录可能含源码、报告和原始素材。清理前请核对文件用途。',style='Notice.TLabel',justify='left',padding=self.pads(8,5))
        notice.pack(fill='x',pady=self.pads(0,7))
        self.wrap_to(notice,18)

        actions = ttk.Frame(right,style='Surface.TFrame')
        actions.pack(fill='x')
        self.rescan_btn = ttk.Button(actions,text='扫描目录',command=self.scan_current)
        self.rescan_btn.pack(side='left')
        ttk.Button(actions,text='打开目录',command=self.open_folder).pack(side='left',padx=self.px(4))
        ttk.Button(actions,text='导出清单',command=self.export).pack(side='left')
        filters = ttk.Frame(right,style='Surface.TFrame',padding=self.pads(0,6))
        filters.pack(fill='x')
        filters.columnconfigure(3,weight=1)
        ttk.Combobox(filters,textvariable=self.category,values=['全部类型',*FILE_TYPES,'其他'],state='readonly',font=('Microsoft YaHei UI',10),width=8).grid(row=0,column=0,sticky='ew',padx=self.pads(0,4))
        ttk.Combobox(filters,textvariable=self.min_size,values=['全部大小','≥ 10 MB','≥ 100 MB','≥ 1 GB'],state='readonly',font=('Microsoft YaHei UI',10),width=8).grid(row=0,column=1,sticky='ew')
        ttk.Label(filters,text='文件名',style='SurfaceMuted.TLabel').grid(row=0,column=2,sticky='w',padx=self.pads(5,3))
        ttk.Entry(filters,textvariable=self.file_query,width=8).grid(row=0,column=3,sticky='ew')


        self.file_tree=self.table(right,[('name','文件名',144),('size','大小',75),('path','相对路径',115)],'extended')
        self.file_tree.bind('<<TreeviewSelect>>',self.update_selection)
        self.file_tree.bind('<Double-1>',self.open_file_row)
        self.folder_keys = {}
        for variable in (self.file_query,self.category,self.min_size):
            variable.trace_add('write',self.schedule_filter)

        selection_bar=ttk.Frame(root,style='Surface.TFrame',padding=self.pads(16,5))

        ttk.Label(selection_bar,textvariable=self.selection_info,style='SurfaceMuted.TLabel').pack(side='left')
        self.clean_btn=ttk.Button(selection_bar,text='移入回收站…',style='Danger.TButton',command=self.clean,state='disabled')
        self.delete_directory_btn=ttk.Button(selection_bar,text='删除整个目录…',style='Danger.TButton',command=lambda:self.clean(True),state='disabled')
        self.delete_directory_btn.pack(side='right')
        self.clean_btn.pack(side='right',padx=(0,8))
        status_label = ttk.Label(root,textvariable=self.status,padding=self.pads(16,4))
        status_label.pack(side='bottom',fill='x')
        selection_bar.pack(side='bottom',fill='x',pady=self.pads(5,0))
        self.wrap_to(status_label,34)
        # Reserve room for the bottom controls before allocating the tables.
        panes.pack(fill='both',expand=True)
        def initial_split():
            if str(self.sidebar) in self.panes.panes():
                self.panes.sashpos(0,round(self.panes.winfo_width()*0.36))
        root.after_idle(initial_split)
        root.bind('<Control-Shift-s>',self.toggle_sidebar)
        self.poll_timer = root.after(100,self.poll)
        if self.db.get() and '--smoke-test' not in sys.argv:root.after(200,self.load)
    def toggle_sidebar(self, event=None):
        if str(self.sidebar) in self.panes.panes():
            self.sidebar_width = self.panes.sashpos(0)
            self.panes.forget(self.sidebar)
            self.sidebar_button.configure(text='显示侧栏')
        else:
            self.panes.insert(0,self.sidebar,weight=1)
            if self.sidebar_width is not None:
                self.panes.sashpos(0,min(self.sidebar_width,self.panes.winfo_width()//2))
            self.sidebar_button.configure(text='隐藏侧栏')
        return 'break'

    def px(self,value):
        return max(1, round(value*self.scale)) if value else 0

    def pads(self,*values):
        return tuple(self.px(value) for value in values)

    def wrap_to(self, label, inset=0):
        def resize(event):
            label.configure(wraplength=max(self.px(80),event.width-self.px(inset)))
        label.bind('<Configure>',resize)

    def configure_theme(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10),background='#f6f6f8',foreground='#252529')
        style.configure('Surface.TFrame',background='white')
        style.configure('Sidebar.TFrame',background='#eceef2')
        style.configure('SidebarSection.TLabel',background='#eceef2',foreground='#31343c',font=('Microsoft YaHei UI',12,'bold'))
        style.configure('SidebarMuted.TLabel',background='#eceef2',foreground='#515662')
        style.configure('Sidebar.Treeview',background='#eceef2',fieldbackground='#eceef2')
        style.configure('Sidebar.Treeview.Heading',background='#eceef2',foreground='#515662')
        style.configure('Brand.TLabel',background='white',foreground='#232327',font=('Microsoft YaHei UI',20,'bold'))
        style.configure('Section.TLabel',background='white',foreground='#18273e',font=('Microsoft YaHei UI',12,'bold'))
        style.configure('SurfaceMuted.TLabel',background='white',foreground='#485a72')
        style.configure('Accent.TFrame',background='#e6e8ed')
        style.configure('Accent.TLabel',background='#e6e8ed',foreground='#4c515d')
        style.configure('Metric.TLabel',background='#e6e8ed',foreground='#24262c',font=('Microsoft YaHei UI',11,'bold'))
        style.configure('Notice.TLabel',background='#fff4d6',foreground='#694500')
        style.configure('Treeview',rowheight=self.px(31),background='white',fieldbackground='white',borderwidth=0,relief='flat')
        style.configure('Treeview.Heading',font=('Microsoft YaHei UI',9,'bold'),background='#f3f3f6',foreground='#44464e',relief='flat',padding=self.pads(6,5))
        for direction in ('Vertical','Horizontal'):
            style.configure(direction+'.TScrollbar',background='#b8bac3',troughcolor='#f2f2f5',
                            bordercolor='#f2f2f5',lightcolor='#b8bac3',darkcolor='#b8bac3',
                            arrowsize=self.px(9),borderwidth=0,relief='flat')
        style.map('Treeview',background=[('selected','#dceaff')],foreground=[('selected','#103c81')])
        style.configure('TEntry',padding=self.pads(6,4),fieldbackground='white',bordercolor='#c7d4e6')
        style.configure('TCombobox',font=('Microsoft YaHei UI',10),padding=self.pads(4,3),bordercolor='#c7d4e6')
        style.map('TCombobox',fieldbackground=[('readonly','white')])
        for role, base, hover, ink in [
            ('TButton','#eeeef2','#e1e2e8','#292b32'),
            ('Blue.TButton','#1857db','#1245b7','white'),
            ('Danger.TButton','#fdebed','#f9d9de','#a61c31'),
        ]:
            style.configure(role,background=base,foreground=ink,padding=self.pads(9,5),
                            width=0,borderwidth=1,relief='flat',bordercolor=base,lightcolor=base,darkcolor=base)
            style.map(role,background=[('disabled','#e6ebf3'),('pressed',hover),('active',hover)],
                      foreground=[('disabled','#66758c'),('!disabled',ink)],
                      bordercolor=[('focus','#527ad2')])

    def table(self,parent,columns,selection):
        box=ttk.Frame(parent);box.pack(fill='both',expand=True);tree=ttk.Treeview(box,columns=[c[0] for c in columns],show='headings',selectmode=selection)
        for name,title,width in columns:tree.heading(name,text=title);tree.column(name,width=self.px(width),minwidth=self.px(54),anchor='e' if name=='size' else 'w',stretch=name!='size')
        if selection == 'browse':
            tree.configure(style='Sidebar.Treeview')
            def fit_conversations(event):
                available = event.width
                size = self.px(76)
                relation = self.px(69)
                tree.column('size',width=size,stretch=False)
                tree.column('shared',width=relation,stretch=False)
                tree.column('title',width=max(self.px(60),available-size-relation),stretch=False)
            tree.bind('<Configure>',fit_conversations)
        tree.tag_configure('stripe',background='#f7f7fa')
        y=ttk.Scrollbar(box,orient='vertical',command=tree.yview);x=ttk.Scrollbar(box,orient='horizontal',command=tree.xview);tree.configure(yscrollcommand=y.set,xscrollcommand=x.set);tree.grid(row=0,column=0,sticky='nsew');y.grid(row=0,column=1,sticky='ns');x.grid(row=1,column=0,sticky='ew');box.rowconfigure(0,weight=1);box.columnconfigure(0,weight=1);return tree
    def settings(self):
        window = tk.Toplevel(self.root)
        window.title('手动选择 ChatGPT 对话目录')
        window.transient(self.root)
        frame = ttk.Frame(window,padding=16)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='本地对话索引或数据目录').pack(anchor='w')
        ttk.Entry(frame,textvariable=self.db,width=72).pack(fill='x',pady=8)
        buttons=ttk.Frame(frame)
        buttons.pack(fill='x')
        for label, action in [('选择目录',self.choose),('选择数据库',self.choose_db),('自动识别',self.auto),('读取对话',self.load)]:
            ttk.Button(buttons,text=label,command=action).pack(side='left',padx=3)
        ttk.Button(buttons,text='关闭',command=window.destroy).pack(side='right')
        ttk.Button(frame,text='如何找到对话目录？',style='Blue.TButton',command=lambda:self.show_directory_help(window)).pack(anchor='w',pady=(10,0))

    def show_directory_help(self, parent):
        window = tk.Toplevel(parent)
        window.title('如何找到 ChatGPT 对话目录？')
        window.transient(parent)
        frame = ttk.Frame(window,padding=18)
        frame.pack(fill='both',expand=True)
        feedback = tk.StringVar()

        def paragraph(text, bold=False):
            font = ('Microsoft YaHei UI',11,'bold') if bold else ('Microsoft YaHei UI',10)
            ttk.Label(frame,text=text,font=font,wraplength=650,justify='left').pack(fill='x',pady=(3,5))

        def path_row(path, allow_use=False):
            row = ttk.Frame(frame)
            row.pack(fill='x',pady=(0,8))
            entry = ttk.Entry(row,width=39,exportselection=False)
            entry.insert(0,path)
            entry.configure(state='readonly')
            entry.pack(side='left',fill='x',expand=True)
            def select_path(event):
                entry.selection_range(0,'end')
                return 'break'

            entry.bind('<Control-a>',select_path)

            def copy_path():
                self.root.clipboard_clear()
                self.root.clipboard_append(path)
                feedback.set('已复制路径。')

            def open_path():
                try:
                    os.startfile(os.path.expandvars(path))
                except OSError:
                    feedback.set('目录打不开，请试用「自动识别」或检查自定义数据路径。')

            def use_path():
                if self.busy:
                    feedback.set('正在扫描，请等本轮统计结束后再切换目录。')
                    return
                self.db.set(path)
                self.load()
                if self.loaded_db and normal(Path(self.loaded_db).parent) == normal(path):
                    window.destroy()

            ttk.Button(row,text='复制路径',command=copy_path).pack(side='left',padx=(6,0))
            ttk.Button(row,text='打开文件夹',command=open_path).pack(side='left',padx=(6,0))
            if allow_use:
                ttk.Button(row,text='使用此目录',style='Blue.TButton',command=use_path).pack(side='left',padx=(6,0))

        paragraph('请选择包含 state_*.sqlite 的文件夹',True)
        paragraph('ChatFiles 用这个索引读取对话名称和对应的工作目录。')
        paragraph('方法一 · 打开默认目录',True)
        path_row(r'%USERPROFILE%\.codex')
        paragraph('1. 点「复制路径」，按 Win + R，粘贴后按 Enter。也可以直接点「打开文件夹」。\n'
                  '2. 在文件夹里找 state_*.sqlite，例如 state_5.sqlite。\n'
                  '3. 回到 ChatFiles，在路径框粘贴目录并点「读取对话」，或点「选择数据库」选中该文件。')
        paragraph('方法二 · 使用本机识别的目录',True)
        found = detect()
        if found:
            path_row(str(Path(found).parent),allow_use=True)
        else:
            paragraph('本机暂未识别到对话索引。可以按下面的步骤手动查找。')
        paragraph('默认目录打不开，或找不到索引？',True)
        paragraph('先回到目录选择窗口点「自动识别」。如果你改过桌面端的数据位置，请打开自定义数据文件夹，确认里面有 state_*.sqlite，再复制资源管理器地址栏的路径。')
        paragraph('源码、图片和实验结果通常在工作目录中。这里应填写对话索引所在目录。只有云端记录的对话暂时无法读取。')
        footer = ttk.Frame(frame)
        footer.pack(fill='x',pady=(7,0))
        ttk.Label(footer,textvariable=feedback,wraplength=530).pack(side='left')
        ttk.Button(footer,text='关闭',command=window.destroy).pack(side='right')

    def open_file_row(self,event):
        if self.file_tree.identify_region(event.x,event.y) != 'cell':
            return
        key = self.file_tree.identify_row(event.y)
        path = self.folder_keys.get(key)
        if not path and key in self.file_keys:
            path = self.file_keys[key][0]
        if not path:
            return
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror('无法打开',f'{path}\n\n{exc}',parent=self.root)

    def sort_by_size(self):
        self.sort_descending = not self.sort_descending
        self.render_threads()

    def schedule_filter(self, *_):
        if self.filter_timer is not None:
            self.root.after_cancel(self.filter_timer)
        self.filter_timer = self.root.after(150,self.apply_filters)

    def apply_filters(self):
        self.filter_timer = None
        self.render_files()

    def update_selection(self, event=None):
        selected = [self.file_keys[key] for key in self.file_tree.selection() if key in self.file_keys]
        total = sum(row[1] for row in selected)
        self.selection_info.set(f'已选 {len(selected):,} 个文件 · {human(total)}' if selected else '未选择文件')
        self.clean_btn.configure(state='normal' if selected and not self.busy else 'disabled')
        can_delete_directory = self.active and not self.busy and os.path.isdir(self.active['path'])
        self.delete_directory_btn.configure(state='normal' if can_delete_directory else 'disabled')

    def choose(self):
        p=filedialog.askdirectory(title='选择 GPT 数据目录（通常是用户目录下 .codex）')
        if p:self.db.set(p);self.load()
    def choose_db(self):
        p=filedialog.askopenfilename(title='选择 state_*.sqlite 对话索引',filetypes=[('SQLite 数据库','*.sqlite')])
        if p:self.db.set(p);self.load()
    def auto(self):self.db.set(detect());self.load()
    def load(self):
        if self.busy:return
        try:
            ds=databases(self.db.get())
            if not ds:raise ValueError('未找到对话数据库。请指定用户数据目录（例如 C:\\Users\\用户名\\.codex），而不是 GPT.exe 所在目录。普通 ChatGPT 的云端对话不能通过此方法读取。')
            self.db.set(str(ds[0]));self.threads=read_threads(ds[0]);self.path_counts={p:sum(t['path']==p for t in self.threads) for p in {t['path'] for t in self.threads}};self.path_related={p:len(conflicts(p,self.threads)) for p in self.path_counts};self.cache.clear();self.active=None;self.files=[];self.file_tree.delete(*self.file_tree.get_children());self.render_threads();self.status.set(f'已读取 {len(self.threads)} 条对话；{len(self.path_counts)} 个工作目录。包含归档对话。')
            self.loaded_db = str(ds[0])
            self.sizes.clear()
            self.byte_totals.clear()
            self.info.set("选择一条对话查看文件。")
            self.file_keys.clear()
            self.update_selection()
            self.render_threads()
            self.scan_all()
        except Exception as ex:messagebox.showerror('读取失败',str(ex))
    def render_threads(self):
        self.update_total()
        selected=self.active['id'] if self.active else None;self.tree.delete(*self.tree.get_children());self.thread_keys={};q=self.query.get().lower()
        threads = self.threads
        if self.sort_descending:
            threads = sorted(threads,key=lambda t:self.byte_totals.get(t['path'],-1),reverse=True)
        for t in threads:
            if q and q not in (t['title']+' '+t['path']).lower():continue
            same=self.path_counts.get(t['path'],1);over=self.path_related.get(t['path'],1);size=self.sizes.get(t['path'],'等待统计')
            flag=f'共享：{same} 条' if same>1 else ('含嵌套关联' if over>1 else '独立目录')
            key=self.tree.insert('','end',values=(('【归档】' if t['archived'] else '')+t['title'],size,flag));self.thread_keys[key]=t
            if t['id']==selected:self.tree.selection_set(key)

    def update_total(self):
        paths = {thread['path'] for thread in self.threads}
        included = set()
        total = waiting = unavailable = partial = nested = 0
        for path in sorted(paths, key=len):
            size = self.byte_totals.get(path)
            if size is None:
                waiting += 1
            elif size < 0:
                unavailable += 1
            elif any(str(parent) in included for parent in Path(path).parents):
                nested += 1
            else:
                total += size
                included.add(path)
                if self.sizes.get(path,'').endswith('（部分）'):
                    partial += 1
        self.total_info.set(f'目录合计（去重）　{human(total) if included else "—"}')
        details = [f'已计入 {len(included)} 个目录']
        shared = len(self.threads) - len(paths)
        if shared or nested:
            details.append(f'合并 {shared} 条共享目录记录、{nested} 个嵌套目录')
        if waiting:
            details.append(f'{waiting} 个目录待统计')
        if unavailable:
            details.append(f'{unavailable} 个目录无法读取')
        if partial:
            details.append(f'{partial} 个目录仅计入已读取部分')
        self.total_detail.set(' · '.join(details))
    def selected(self,event=None):
        sels=self.tree.selection()
        if not sels:return
        self.active=self.thread_keys[sels[0]];self.show_files()
        if self.active['path'] not in self.cache and not self.busy:self.scan_current()
    def show_files(self):
        self.file_tree.delete(*self.file_tree.get_children());self.file_keys={}
        if not self.active:return
        p=self.active['path'];related=conflicts(p,self.threads);self.files=self.cache.get(p,([],[]))[0]
        count_text=f'{len(self.files):,} 个文件 / {self.sizes.get(p,"等待统计")}' if p in self.cache else '等待加载文件列表'
        self.info.set(self.active['title']+'\n'+p+f'\n{count_text} · 关联 {len(related)} 条对话'+(' · 共享或嵌套目录' if len(related)>1 else ''))
        self.render_files()

    def render_files(self):
        self.file_tree.delete(*self.file_tree.get_children())
        self.file_keys.clear()
        self.folder_keys.clear()
        if not self.active:
            self.update_selection()
            return
        folder = self.active['path']
        query = self.file_query.get().strip().casefold()
        category = self.category.get()
        minimum = {'≥ 10 MB':10*1024**2,'≥ 100 MB':100*1024**2,'≥ 1 GB':1024**3}.get(self.min_size.get(),0)
        if category == '全部类型' and not minimum:
            try:
                with os.scandir(folder) as entries:
                    subfolders = sorted(
                        (e for e in entries if e.is_dir(follow_symlinks=False) and not e.is_junction()),
                        key=lambda e:e.name.casefold())
                for entry in subfolders:
                    if query in entry.path.casefold():
                        key = self.file_tree.insert('','end',values=(entry.name,'文件夹',entry.name))
                        self.folder_keys[key] = entry.path
            except OSError:
                pass
        count = 0
        for row in self.files:
            path, size, _ = row
            if size < minimum or query not in path.casefold():
                continue
            if category != '全部类型' and file_type(path) != category:
                continue
            if count >= 15000:
                self.status.set('结果超过 15,000 项，请继续筛选或导出清单。')
                break
            key = self.file_tree.insert('','end',values=(os.path.basename(path),human(size),os.path.relpath(path,folder)),tags=('stripe',) if count % 2 else ())
            self.file_keys[key] = row
            count += 1
        self.update_selection()

    def run(self,paths,summary=False):
        if self.busy:return
        self.busy=True;self.scan_all_btn.configure(state='disabled');self.clean_btn.configure(state='disabled');self.delete_directory_btn.configure(state='disabled');self.rescan_btn.configure(state='disabled');self.status.set('正在只读统计…')
        def worker():
            try:
                for i,p in enumerate(paths):
                    self.events.put(('progress',f'统计 {i+1}/{len(paths)}：{p}'))
                    if not os.path.isdir(p):
                        self.events.put(('size',(p,'目录不可用',-1)))
                        continue
                    files,errors=scan(p)
                    total=sum(f[1] for f in files)
                    size=human(total)
                    if errors:size=size+'（部分）' if files else '读取失败'
                    self.events.put(('size',(p,size,total if files or not errors else -1)))
                    if not summary:self.events.put(('result',(p,(files,errors))))
            finally:
                self.events.put(('done',summary))
        threading.Thread(target=worker,daemon=True).start()
    def scan_current(self):
        if self.active:self.run([self.active['path']])
    def scan_all(self):self.run(list(dict.fromkeys(t['path'] for t in self.threads)),summary=True)
    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress':self.status.set(value)
                elif kind=='size':
                    path,size,total=value
                    self.byte_totals[path]=total
                    self.sizes[path]=size
                    self.update_total()
                    for key,thread in self.thread_keys.items():
                        if thread['path']==path:self.tree.set(key,'size',size)
                elif kind=='result':self.cache[value[0]]=value[1]
                elif kind=='done':
                    self.busy=False;self.scan_all_btn.configure(state='normal');self.rescan_btn.configure(state='normal');self.show_files();self.status.set('大小统计完成。')
                    if value and self.sort_descending:self.render_threads()
                    if value and self.active and self.active['path'] not in self.cache:self.scan_current()
        except queue.Empty:pass
        self.poll_timer = self.root.after(100,self.poll)
    def open_folder(self):
        if self.active and os.path.isdir(self.active['path']):os.startfile(self.active['path'])
    def export(self):
        if not self.active or self.active['path'] not in self.cache:return
        p=filedialog.asksaveasfilename(defaultextension='.txt',initialfile='对话文件清单.txt',filetypes=[('文本清单','*.txt')])
        if not p:return
        with open(p,'w',encoding='utf-8-sig',newline='') as f:
            f.write('对话：'+self.active['title']+'\n目录：'+self.active['path']+'\n目录关联不证明文件创建来源。\n')
            for r in self.files:f.write(f'{human(r[1])}\t{r[1]}\t{r[0]}\n')
        self.status.set('已导出全部文件记录。')
    def clean(self,whole_directory=False):
        if self.busy or not self.active:return
        workspace = self.active['path']
        data_home = str(Path(self.loaded_db or self.db.get()).parent)
        selected=[workspace] if whole_directory else [self.file_keys[k][0] for k in self.file_tree.selection() if k in self.file_keys]
        if not selected:messagebox.showinfo('请选择文件','请在右侧选择具体文件，可使用 Ctrl / Shift 多选。');return
        paths=[];skipped=[]
        for p in selected:
            try:paths.append(protect(p,workspace,data_home,whole_directory=whole_directory))
            except ValueError as ex:skipped.append(p+'：'+str(ex))
        if not paths:
            self.show_cleanup_result(workspace,'软件未执行清理','\n'.join(skipped[:10]),protected=True)
            return
        related=conflicts(self.active['path'],self.threads)
        win=tk.Toplevel(self.root);win.title('确认具体清理范围');win.geometry(f'{min(self.px(780),desktop_size(self.root)[0]-32)}x{min(self.px(540),desktop_size(self.root)[1]-60)}');win.transient(self.root);win.grab_set()
        ttk.Label(win,text='准备将整个工作目录及其内容移入回收站' if whole_directory else f'准备将 {len(paths)} 个文件移入回收站',font=('Microsoft YaHei UI',14,'bold')).pack(anchor='w',padx=16,pady=12)
        if whole_directory:
            ttk.Label(win,text=WORKSPACE_NOTICE+' 整目录清理会一并移动其中的源码、成果及其他文件。',
                      background='#fff1c8',foreground='#694500',wraplength=710,
                      justify='left',padding=(10,7)).pack(fill='x',padx=16,pady=(0,8))
        ttk.Label(win,text=f'不会清除对话本身。受保护/已不存在的 {len(skipped)} 项将保留。若 Windows 提示永久删除，请取消。',wraplength=740).pack(anchor='w',padx=16)
        text=tk.Text(win,wrap='word',height=18);text.pack(fill='both',expand=True,padx=16,pady=10);text.insert('end','关联对话：\n'+'\n'.join(t['title'] for t in related)+'\n\n将处理的路径：\n'+'\n'.join(paths)+'\n\n跳过项：\n'+'\n'.join(skipped));text.configure(state='disabled')
        accept=tk.BooleanVar()
        ttk.Checkbutton(win,text='我已核对清理范围和关联对话',variable=accept).pack(anchor='w',padx=16)
        def execute():
            if not accept.get():messagebox.showinfo('需要核对','请先核对范围并勾选。',parent=win);return
            try:
                checked=[protect(p,workspace,data_home,whole_directory=whole_directory) for p in paths]
            except (ValueError,OSError) as ex:
                self.show_cleanup_result(workspace,'软件未执行清理',str(ex),protected=True,parent=win)
                return
            failure = None
            try:
                recycle(checked)
            except (RuntimeError,OSError) as ex:
                failure = str(ex)
            win.destroy()
            self.cache.clear()
            try:
                if whole_directory:
                    for thread in related:
                        path = thread['path']
                        self.sizes.pop(path,None)
                        self.byte_totals.pop(path,None)
                    self.render_threads()
                    self.scan_all()
                else:
                    self.scan_current()
            except (tk.TclError,OSError) as ex:
                messagebox.showwarning('列表刷新失败',f'回收站操作已结束，请手动刷新大小。\n{ex}',parent=self.root)
            if failure:
                self.show_cleanup_result(workspace,'清理未全部完成',failure)
        buttons=ttk.Frame(win);buttons.pack(fill='x',padx=16,pady=12);cancel = ttk.Button(buttons,text='取消',command=win.destroy)
        cancel.pack(side='right')
        confirm = ttk.Button(buttons,text='确认删除整个目录' if whole_directory else '确认移入回收站',style='Danger.TButton',command=execute,state='disabled')
        confirm.pack(side='right',padx=8)
        accept.trace_add('write',lambda *_:confirm.configure(state='normal' if accept.get() else 'disabled'))
        win.bind('<Escape>',lambda event:win.destroy())
        cancel.focus_set()

    def show_cleanup_result(self, workspace, title, reason, *, protected=False, parent=None):
        owner = parent or self.root
        window = tk.Toplevel(owner)
        window.title(title)
        window.transient(owner)
        frame = ttk.Frame(window,padding=16)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text=title,font=('Microsoft YaHei UI',13,'bold')).pack(anchor='w',pady=(0,8))
        ttk.Label(frame,text=reason,wraplength=640,justify='left').pack(fill='x')
        entry = ttk.Entry(frame,width=80)
        entry.insert(0,workspace)
        entry.configure(state='readonly')
        entry.pack(fill='x',pady=8)
        if protected:
            guide = '按上面的提示处理。打开目录后，可以核对其中的项目文件和实际工作位置。'
        else:
            guide = '可能已有部分文件移入回收站。请先核对剩余文件，也可以打开目标目录，在资源管理器中手动删除确认不需要的文件。'
        ttk.Label(frame,text=guide,wraplength=640,justify='left').pack(fill='x',pady=(0,8))
        feedback = tk.StringVar()

        def open_directory(path):
            try:
                os.startfile(str(path))
            except OSError as ex:
                feedback.set(f'无法打开该目录。{ex}')

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(workspace)
            feedback.set('已复制完整路径。')

        actions = ttk.Frame(frame)
        actions.pack(fill='x')
        ttk.Button(actions,text='打开目标目录',command=lambda:open_directory(workspace)).pack(side='left')
        ttk.Button(actions,text='打开上级目录',command=lambda:open_directory(Path(workspace).parent)).pack(side='left',padx=6)
        ttk.Button(actions,text='复制路径',command=copy).pack(side='left')
        ttk.Button(actions,text='关闭',command=window.destroy).pack(side='right')
        ttk.Label(frame,textvariable=feedback,wraplength=640).pack(fill='x',pady=(6,0))

if __name__=='__main__':
    configure_windows_dpi()
    root=tk.Tk()
    if '--smoke-test' in sys.argv:
        root.withdraw();app=App(root);app.load();root.update()
        Path(sys.argv[sys.argv.index('--smoke-test')+1]).write_text(json.dumps(dict(version=VERSION,threads=len(app.threads),ui=True)),encoding='utf-8');root.destroy()
    else:
        app=App(root);root.mainloop()
