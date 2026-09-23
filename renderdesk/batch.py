"""Recognize sequential Python render plans without executing their source."""
import ast
from pathlib import Path
import tokenize
import psutil
from .adoption import ScriptReader, Unknown, dotted, infer_script
from .engine.protocol import signature


def identity(item):
    return {'pid': item.pid, 'created': item.create_time()}


def python_script(item):
    args = item.cmdline()
    if not Path(item.exe()).stem.lower().startswith(('python', 'pypy')):
        return None
    # Only a direct script invocation; -m, -c and interpreter options are not inferred.
    if len(args) < 2 or args[1].startswith('-') or not args[1].lower().endswith('.py'):
        return None
    path = (Path(item.cwd()) / args[1]).resolve()
    return path if path.is_file() else None


class PlanReader(ScriptReader):
    def expr(self, node):
        if isinstance(node, ast.Call) and dotted(node.func) in ('min', 'max') and not node.keywords:
            values = [self.expr(n) for n in node.args]
            return (min if dotted(node.func) == 'min' else max)(*values)
        return super().expr(node)

    def commands(self, tree):
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and dotted(n.func) == 'subprocess.Popen']
        if len(calls) != 1:
            raise Unknown('需要一个顺序执行的 subprocess.Popen 批次循环')
        launch = calls[0]
        loops = [n for n in tree.body if isinstance(n, ast.For) and launch in ast.walk(n)]
        if len(loops) != 1 or not isinstance(loops[0].target, ast.Name):
            raise Unknown('分批循环必须位于脚本顶层')
        loop = loops[0]
        assignments=[n for n in ast.walk(loop) if isinstance(n,ast.Assign) and n.value is launch]
        if len(assignments)!=1 or len(assignments[0].targets)!=1 or not isinstance(assignments[0].targets[0],ast.Name):
            raise Unknown('需要保存子进程句柄并等待它退出')
        handle=assignments[0].targets[0].id
        waits=[n for n in ast.walk(loop) if isinstance(n,ast.While) and isinstance(n.test,ast.Compare)
               and len(n.test.ops)==1 and isinstance(n.test.ops[0],ast.Is)
               and isinstance(n.test.left,ast.Call) and dotted(n.test.left.func)==handle+'.poll'
               and isinstance(n.test.comparators[0],ast.Constant) and n.test.comparators[0].value is None]
        if len(waits)!=1 or waits[0].lineno<=launch.lineno:
            raise Unknown('需要明确等待本批子进程退出，暂不支持并行调度')
        if any(isinstance(n,(ast.Break,ast.Continue)) for n in ast.walk(waits[0])):
            raise Unknown('等待过程会提前跳出，不能确认顺序调度')
        for n in tree.body[:tree.body.index(loop)]:
            if isinstance(n, ast.Assign):
                for target in n.targets:
                    self.assign(target, n.value)
            elif isinstance(n, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Expr)):
                continue
            else:
                raise Unknown('批次配置依赖动态控制流程')
        values = self.expr(loop.iter)
        if not isinstance(values, range) or not 1 <= len(values) <= 2000:
            raise Unknown('批次循环需要固定 range，最多 2000 批')
        results = []
        def walk(nodes, log_path=None):
            for n in nodes:
                if isinstance(n, ast.Assign):
                    if n.value is launch:
                        if launch.keywords and any(k.arg not in ('stdout','stderr','stdin','creationflags') for k in launch.keywords):
                            raise Unknown('子进程自定义 cwd/env/shell 暂不支持')
                        if len(launch.args) != 1:
                            raise Unknown('无法确定子进程启动参数')
                        args = self.expr(launch.args[0])
                        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
                            raise Unknown('子进程参数必须为字符串列表')
                        results.append({'args': args, 'log': log_path})
                    else:
                        for target in n.targets:
                            self.assign(target, n.value)
                elif isinstance(n, ast.With):
                    if len(n.items) != 1:
                        raise Unknown('不支持的批次日志配置')
                    call = n.items[0].context_expr
                    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute) or call.func.attr != 'open':
                        raise Unknown('批次上下文不是日志文件')
                    path = self.expr(call.func.value)
                    walk(n.body, str((self.cwd / path).resolve()))
                elif launch in ast.walk(n):
                    raise Unknown('子进程启动依赖条件或嵌套循环')
                elif isinstance(n, ast.If):
                    if any(isinstance(x, ast.Continue) for x in ast.walk(n)):
                        if n.orelse or len(n.body) != 1 or not isinstance(n.body[0], ast.Continue) or not isinstance(n.test, ast.Call) or dotted(n.test.func) != 'all':
                            raise Unknown('只能跳过已经存在的整批帧')
                    elif any(isinstance(x, ast.Break) for x in ast.walk(n)):
                        raise Unknown('不能确定循环提前退出后的计划范围')
                    if any(isinstance(x, (ast.Assign,ast.AugAssign,ast.AnnAssign)) for x in ast.walk(n)):
                        raise Unknown('批次参数依赖运行时分支')
                elif isinstance(n, ast.While):
                    if any(isinstance(x, (ast.Assign,ast.AugAssign,ast.AnnAssign)) for x in ast.walk(n)):
                        raise Unknown('等待子进程期间修改批次参数暂不支持')
                elif isinstance(n, (ast.For, ast.Try, ast.Break, ast.Continue)):
                    raise Unknown('批次循环包含不支持的控制流')
        for value in values:
            self.env[loop.target.id] = value
            before = len(results)
            walk(loop.body)
            if len(results) != before + 1:
                raise Unknown('每批必须恰好启动一个 Blender')
        return results


def infer_batch(child, scan):
    """Return a verified parent plan, or None for a non-script parent."""
    parent = child.parent()
    if not parent:
        return None
    path = python_script(parent)
    if not path:
        return None
    source = signature(path)
    if source['size'] > 1024 * 1024:
        raise ValueError('外层脚本过大，无法识别')
    with tokenize.open(path) as stream:
        tree = ast.parse(stream.read())
    if not any(isinstance(n, ast.Call) and dotted(n.func) == 'subprocess.Popen' for n in ast.walk(tree)):
        return None
    if source['mtime_ns'] / 1e9 > parent.create_time() + 2:
        raise ValueError('外层脚本在启动后被修改，无法确认运行中的计划')
    protected = {'Path','str','int','range','min','max','all','len','list','tuple','subprocess','sys','pathlib'}
    for n in ast.walk(tree):
        if (isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in protected or
            isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store) and n.id in protected or
            isinstance(n, ast.Call) and dotted(n.func) in ('exec','eval','__import__')):
            raise ValueError('外层脚本重定义基础名称或动态执行代码，无法确认计划')
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ('unlink','rmdir','rmtree','remove','truncate'):
            raise ValueError('外层脚本包含删除文件的操作，不能自动重启续跑')
    reader = PlanReader(path, parent.cmdline()[1:], parent.cwd(), scan)
    try:
        batches = reader.commands(tree)
        contract = None
        expected = None
        all_frames = 0
        for batch in batches:
            args = batch['args']
            if args.count('--python') != 1 or not any(x in args for x in ('-b','--background')) or any(x in args for x in ('-a','-f','--render-anim','--render-frame','--python-expr')):
                raise Unknown('每批需要单个 --python 同步逐帧渲染')
            script = (Path(parent.cwd()) / args[args.index('--python')+1]).resolve()
            blend = next((a for a in args if a.lower().endswith('.blend')), '')
            if (Path(parent.cwd()) / blend).resolve() != Path(scan['blend']).resolve() or Path(args[0]).resolve() != Path(scan['blender']).resolve():
                raise Unknown('批次使用不同工程或 Blender')
            config = infer_script(script, args, parent.cwd(), scan)
            with tokenize.open(script) as stream:
                child_tree=ast.parse(stream.read())
            if not any(isinstance(n,ast.If) and ScriptReader.skip_saved(n) for n in ast.walk(child_tree)):
                raise Unknown('续跑计划需要子脚本跳过已经保存的帧')
            if any(isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in ('unlink','rmdir','rmtree','remove','truncate') for n in ast.walk(child_tree)):
                raise Unknown('子脚本包含删除输出的操作，不能重启续跑')
            same = (config['output'],config['pattern'],config['scene'],str(script),config['step'])
            if contract is not None and same != contract:
                raise Unknown('各批次输出、场景或步长不一致')
            if expected is not None and config['start'] != expected:
                raise Unknown('批次帧范围存在重叠或空洞')
            expected = config['end'] + config['step']
            all_frames += len(range(config['start'], expected, config['step']))
            if all_frames > 100000:
                raise Unknown('整个计划最多支持 100000 帧')
            contract = same
            batch.update(start=config['start'], end=config['end'], step=config['step'])
        actual = child.cmdline()
        if not any(command_matches(actual,b['args']) for b in batches):
            raise Unknown('当前 Blender 参数不属于识别出的计划')
        if signature(path) != source:
            raise Unknown('识别期间外层脚本发生变化')
        return {'script':str(path),'source':source,'args':parent.cmdline(),'cwd':parent.cwd(),
                'identity':identity(parent),'batches':batches,'start':batches[0]['start'],
                'end':batches[-1]['end'],'step':batches[0]['step']}
    except (Unknown, ValueError, KeyError, TypeError, IndexError) as error:
        raise ValueError('暂不能接管外层脚本：'+str(error)) from error


def command_matches(actual, expected):
    if len(actual) != len(expected):
        return False
    def normalized(value):
        return str(value).replace('\\','/').casefold()
    return all(normalized(a) == normalized(b) for a,b in zip(actual,expected))


def current_children(plan, parent):
    """Only direct Blender children with the exact statically recognized command."""
    result=[]
    if parent:
        for item in parent.children():
            try:
                match=next((b for b in plan['batches'] if command_matches(item.cmdline(),b['args'])),None)
                if match: result.append((item,match))
                elif item.name().lower() in ('blender','blender.exe'):
                    raise ValueError('外层脚本启动了计划以外的 Blender，已停止自动操作，请检查脚本')
            except psutil.Error:
                continue
    if len(result)>1:
        raise ValueError('发现多个并行 Blender，当前计划只支持顺序调度，已停止自动操作')
    return result
