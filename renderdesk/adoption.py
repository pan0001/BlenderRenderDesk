"""Infer a supported script's render contract without importing or executing it.

Only bounded, pure Python expressions are interpreted. Disk scripts and scene
metadata are evidence, not access to the other Blender process's Python memory.
"""
import ast
import operator
from pathlib import Path
import re
from .engine.protocol import signature, png_complete


class Unknown(ValueError):
    pass


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return base + '.' + node.attr if base else ''
    return ''


class ScriptReader:
    def __init__(self, script, argv, cwd, scan):
        self.script, self.cwd, self.scan = Path(script).resolve(), Path(cwd), scan
        self.env = {'__file__': str(self.script), '__name__': '__main__', 'sys.argv': argv}
        self.scenes = {s['scene']: s for s in scan['scenes']}
        self.scene = scan['active_scene']
        self.aliases = {}
        self.settings = {}
        self.current_frames = {}
        self.outputs = []
        self.budget = 500000

    def expr(self, node):
        self.budget -= 1
        if self.budget < 0:
            raise Unknown('脚本计算超出自动识别预算')
        if isinstance(node, ast.Constant):
            return node.value
        name = dotted(node)
        if name in self.env:
            return self.env[name]
        if name and '.' in name:
            alias, attr = name.split('.', 1)
            if alias in self.aliases and attr in ('frame_start', 'frame_end', 'frame_step'):
                return self.scenes[self.aliases[alias]][{'frame_start':'start','frame_end':'end','frame_step':'step'}[attr]]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [self.expr(n) for n in node.elts]
        if isinstance(node, ast.Subscript):
            value = self.expr(node.value)
            if isinstance(node.slice, ast.Slice):
                parts = [self.expr(n) if n else None for n in (node.slice.lower,node.slice.upper,node.slice.step)]
                return value[slice(*parts)]
            return value[self.expr(node.slice)]
        if isinstance(node, ast.IfExp):
            return self.expr(node.body if self.expr(node.test) else node.orelse)
        if isinstance(node, ast.UnaryOp):
            value = self.expr(node.operand)
            if isinstance(node.op, ast.Not): return not value
            if isinstance(node.op, ast.USub): return -value
            if isinstance(node.op, ast.UAdd): return +value
        if isinstance(node, ast.BoolOp):
            for part in node.values:
                value = self.expr(part)
                if isinstance(node.op, ast.And) and not value: return value
                if isinstance(node.op, ast.Or) and value: return value
            return value
        if isinstance(node, ast.Compare):
            value = self.expr(node.left)
            ops = {ast.Eq:operator.eq,ast.NotEq:operator.ne,ast.Lt:operator.lt,ast.LtE:operator.le,ast.Gt:operator.gt,ast.GtE:operator.ge,ast.In:lambda a,b:a in b,ast.NotIn:lambda a,b:a not in b}
            for op, right in zip(node.ops,node.comparators):
                other=self.expr(right)
                if type(op) not in ops: raise Unknown('未知比较')
                if not ops[type(op)](value,other): return False
                value=other
            return True
        if isinstance(node, ast.BinOp):
            left,right=self.expr(node.left),self.expr(node.right)
            if isinstance(left,str) and isinstance(node.op,ast.Mod) and any(int(n)>32 for n in re.findall(r'%0?(\d+)',left)):
                raise Unknown('格式过长')
            ops={ast.Add:operator.add,ast.Sub:operator.sub,ast.Div:operator.truediv,ast.Mod:operator.mod}
            if type(node.op) in ops:
                result=ops[type(node.op)](left,right)
                if isinstance(result,str) and len(result)>32768: raise Unknown('路径过长')
                return result
        if isinstance(node, ast.JoinedStr):
            parts=[]
            for part in node.values:
                if isinstance(part,ast.Constant): parts.append(str(part.value))
                else:
                    value=self.expr(part.value)
                    if part.conversion==114: value=repr(value)
                    elif part.conversion==115: value=str(value)
                    spec=self.expr(part.format_spec) if part.format_spec else ''
                    if len(spec)>20 or any(int(n)>32 for n in re.findall(r'\d+',spec)): raise Unknown('格式过长')
                    parts.append(format(value,spec))
            return ''.join(parts)
        if isinstance(node, ast.Attribute):
            value=self.expr(node.value)
            if isinstance(value,Path) and node.attr in ('parent','name','stem','suffix','parents'):
                return getattr(value,node.attr)
        if isinstance(node, ast.Call):
            args=[self.expr(a) for a in node.args]
            if node.keywords: raise Unknown('不支持的表达式关键字')
            name=dotted(node.func)
            if name in ('Path','pathlib.Path'):
                return Path(*args)
            if name in ('str','int','len','list','tuple','range'):
                if name=='range':
                    value=range(*args)
                    if len(value)>100000: raise Unknown('最多支持 100000 帧')
                    return value
                if name in ('list','tuple'):
                    if len(args[0])>100000: raise Unknown('序列过长')
                    return list(args[0])
                return {'str':str,'int':int,'len':len}[name](*args)
            if name=='os.path.join': return str(Path(*args))
            if name=='os.path.dirname': return str(Path(args[0]).parent)
            if name in ('os.path.abspath','os.path.realpath'): return str((self.cwd/args[0]).resolve())
            if name=='bpy.path.abspath':
                value=str(args[0])
                return str((Path(self.scan['blend']).parent/value[2:]).resolve()) if value.startswith('//') else str((self.cwd/value).resolve())
            if isinstance(node.func,ast.Attribute):
                base=self.expr(node.func.value);method=node.func.attr
                if isinstance(base,Path) and method=='resolve': return (self.cwd/base).resolve()
                if isinstance(base,Path) and method in ('joinpath','with_name','with_suffix'): return getattr(base,method)(*args)
                if isinstance(base,(list,tuple)) and method=='index': return base.index(*args)
                if isinstance(base,str) and method in ('zfill','replace','format'):
                    if method=='zfill' and args[0]>32: raise Unknown('格式过长')
                    if method=='format' and any(int(n)>32 for n in re.findall(r':0?(\d+)',base)): raise Unknown('格式过长')
                    if method=='format':
                        from string import Formatter
                        for _,field,spec,_ in Formatter().parse(base):
                            if '{' in spec or '}' in spec or len(spec)>20:
                                raise Unknown('不支持动态格式宽度')
                    return getattr(base,method)(*args)
        raise Unknown('表达式依赖运行时变量')

    def scene_ref(self,node):
        name=dotted(node)
        if name in ('bpy.context.scene','bpy.context.window.scene'): return self.scene
        if name in self.aliases: return self.aliases[name]
        if isinstance(node,ast.Subscript) and dotted(node.value)=='bpy.data.scenes':
            scene=self.expr(node.slice)
            if scene not in self.scenes: raise Unknown('场景不在工程中')
            return scene
        return None

    def assign(self,target,value):
        name=dotted(target)
        if isinstance(target,ast.Name):
            try: scene=self.scene_ref(value)
            except (Unknown,KeyError,TypeError,ValueError): scene=None
            if scene:
                self.aliases[name]=scene
                return
            self.aliases.pop(name,None)
        if name=='bpy.context.window.scene':
            scene=self.scene_ref(value)
            if not scene: raise Unknown('无法确定渲染场景')
            self.scene=scene
            return
        known=True
        try: result=self.expr(value)
        except (Unknown,KeyError,TypeError,ValueError,IndexError,OverflowError):
            result=None;known=False
        if name:
            self.env.pop(name,None)
            if known: self.env[name]=result
            head,_,tail=name.partition('.')
            if head in self.aliases:
                self.settings[(self.aliases[head],tail)]=result

    @staticmethod
    def has_render(nodes):
        return any(isinstance(n,ast.Call) and dotted(n.func)=='bpy.ops.render.render' for root in nodes for n in ast.walk(root))

    @staticmethod
    def skip_saved(node):
        # Derive the intended complete frame range, including already saved frames.
        return (isinstance(node.test,ast.Call) and isinstance(node.test.func,ast.Attribute)
                and node.test.func.attr=='exists' and len(node.body)==1
                and isinstance(node.body[0],ast.Continue) and not node.orelse)

    def block(self,nodes):
        for node in nodes:
            if isinstance(node,(ast.Import,ast.ImportFrom,ast.FunctionDef,ast.ClassDef)): continue
            if isinstance(node,ast.Assign):
                for target in node.targets: self.assign(target,node.value)
            elif isinstance(node,ast.AnnAssign) and node.value:
                self.assign(node.target,node.value)
            elif isinstance(node,ast.AugAssign):
                self.assign(node.target,ast.Name(id='_unresolved_'))
            elif isinstance(node,ast.If):
                if self.skip_saved(node): continue
                try: decision=self.expr(node.test)
                except (Unknown,TypeError,KeyError,IndexError):
                    if self.has_render([node]) or any(isinstance(n,(ast.Continue,ast.Break,ast.Return,ast.Raise)) for n in ast.walk(node)):
                        raise Unknown('渲染分支依赖未知运行状态')
                    # Invalidate uncertain assignments rather than guessing either branch.
                    for n in ast.walk(node):
                        if isinstance(n,ast.Assign):
                            for t in n.targets: self.assign(t,ast.Name(id='_unresolved_'))
                    continue
                self.block(node.body if decision else node.orelse)
            elif isinstance(node,ast.For):
                if not self.has_render(node.body):
                    for n in ast.walk(node):
                        if isinstance(n,ast.Assign):
                            for t in n.targets: self.assign(t,ast.Name(id='_unresolved_'))
                    continue
                values=self.expr(node.iter)
                if not isinstance(values,(list,tuple,range)) or not 1<=len(values)<=100000: raise Unknown('无法确定帧序列')
                if not isinstance(node.target,ast.Name): raise Unknown('不支持的循环变量')
                for value in values:
                    self.env[node.target.id]=value
                    self.block(node.body)
            elif isinstance(node,ast.Expr) and isinstance(node.value,ast.Call):
                call=node.value;name=dotted(call.func)
                if name=='bpy.ops.render.render': self.capture(call)
                elif name.endswith('.frame_set') and name.split('.')[0] in self.aliases:
                    self.current_frames[self.aliases[name.split('.')[0]]]=self.expr(call.args[0])
            elif isinstance(node,(ast.Break,ast.Continue,ast.Return,ast.While,ast.Try,ast.With)):
                raise Unknown('渲染流程包含暂不支持的控制语句')

    def capture(self,call):
        kw={k.arg:self.expr(k.value) for k in call.keywords}
        if call.args or not kw.get('write_still') or kw.get('animation'):
            raise Unknown('当前只支持同步逐帧 PNG 脚本')
        scene=kw.get('scene',self.scene)
        config=self.scenes[scene]
        fmt=self.settings.get((scene,'render.image_settings.file_format'),config['format'])
        if fmt!='PNG': raise Unknown('脚本实际输出不是 PNG 序列')
        if self.settings.get((scene,'render.use_multiview'),config.get('use_multiview',False)):
            raise Unknown('当前不支持多视图渲染接管')
        path=self.settings.get((scene,'render.filepath'))
        if not isinstance(path,(str,Path)) or not path: raise Unknown('无法确定脚本实际输出路径')
        frame=self.current_frames.get(scene)
        if not isinstance(frame,int) or isinstance(frame,bool): raise Unknown('无法确定实际帧号')
        path=str(path)
        path=(Path(self.scan['blend']).parent/path[2:] if path.startswith('//') else self.cwd/path).resolve()
        extension=self.settings.get((scene,'render.use_file_extension'),config.get('use_file_extension',True))
        if path.suffix.lower()!='.png' and extension: path=Path(str(path)+'.png')
        if path.suffix.lower()!='.png': raise Unknown('实际输出不是 .png 文件')
        self.outputs.append((frame,path,scene))

    def read(self):
        if self.script.stat().st_size>1024*1024: raise Unknown('脚本超过自动识别大小限制')
        import tokenize
        with tokenize.open(self.script) as stream: tree=ast.parse(stream.read())
        protected={'Path','str','int','len','list','tuple','range','bpy','sys','os','pathlib'}
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.ClassDef)) and node.name in protected:
                raise Unknown('脚本重定义了自动识别所需的基础名称')
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Store) and node.id in protected:
                raise Unknown('脚本覆盖了自动识别所需的基础名称')
            if isinstance(node,ast.Call) and dotted(node.func) in ('eval','exec','__import__'):
                raise Unknown('脚本包含动态执行代码，无法静态确定输出')
        self.block(tree.body)
        if not self.outputs: raise Unknown('未找到可识别的同步逐帧渲染循环')
        sequence=[f for f,_,_ in self.outputs]
        step=sequence[1]-sequence[0] if len(sequence)>1 else 1
        if step<1 or sequence!=list(range(sequence[0],sequence[-1]+1,step)):
            raise Unknown('当前接管需要连续或固定步长的帧序列')
        folder=self.outputs[0][1].parent;scene=self.outputs[0][2]
        if any(p.parent!=folder or s!=scene for _,p,s in self.outputs): raise Unknown('单次接管暂不支持多个输出目录或场景')
        first=self.outputs[0][1].name
        candidates=[]
        for match in re.finditer(r'-?\d+',first):
            width=len(match.group());prefix,suffix=first[:match.start()],first[match.end():]
            if all(p.name==prefix+f'{f:0{width}d}'+suffix for f,p,_ in self.outputs): candidates.append(prefix+'#'*width+suffix)
        if len(candidates)!=1: raise Unknown('无法唯一确定帧文件名模板')
        if not folder.is_dir(): raise Unknown('脚本实际输出目录尚未创建，请稍后重试')
        confirmed=any(p.is_file() and png_complete(p) for _,p,_ in self.outputs)
        project={k:v for k,v in self.scenes[scene].items() if k!='project_paths'}
        return {'blend':self.scan['blend'],'blender':self.scan['blender'],'scene':scene,
                'project':project,'source':self.scan['source'],'start':sequence[0],'end':sequence[-1],
                'step':step,'output':str(folder),'pattern':candidates[0],'format':'PNG','threads':0,
                'range_source':'script','auto_detected':True,'detection':'脚本与启动参数识别，'+('已核对实际 PNG' if confirmed else '等待首帧保存')}


def infer_script(script,argv,cwd,scan):
    before=signature(script)
    try:
        values=ScriptReader(script,argv,cwd,scan).read()
    except (Unknown,SyntaxError,KeyError,TypeError,ValueError,IndexError,RecursionError,OverflowError) as error:
        raise ValueError('暂无法自动接管：'+str(error)) from error
    if signature(script)!=before: raise ValueError('识别期间脚本发生变化，请重试')
    values['detected_script_source']=before
    return values
