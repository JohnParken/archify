"""Native comparison, artifact checks, and explicit recovery command adapters."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid

from .common import DiagnosticError, diagnostic, diagnostic_scope
from .integrity import CONTRACT, identity, read_regular, receipt_path, verify_bytes


def advanced_main(argv):
    argv = list(argv)
    from .cli import _command
    command = _command(argv)
    result = {'schemaVersion': 1, 'runtime': 'python', 'command': command,
              'ok': False, 'diagnostics': []}
    json_mode='--json' in argv
    stage = 'arguments'
    with diagnostic_scope() as recorded:
        try:
            from .cli import Parser, ArgumentError, _main, js_integer

            parser = Parser()
            parser.add_argument('command')
            parser.add_argument('paths', nargs='*')
            parser.add_argument('--json', action='store_true')
            parser.add_argument('--summary', action='store_true')
            parser.add_argument('--quality', choices=['standard', 'showcase'])
            parser.add_argument('--repo-root')
            parser.add_argument('--receipt')
            parser.add_argument('--candidate-sha256')
            parser.add_argument('--out-dir')
            parser.add_argument('--require-provenance', action='store_true')
            parser.add_argument('--browser', choices=['off', 'auto', 'required'], default='off')
            parser.add_argument('--open', action='store_true')
            parser.add_argument('--layout-json', action='store_true')
            args = parser.parse_intermixed_args(argv)
            command = args.command
            result['command'] = command

            diagram_types = {'sequence', 'dataflow', 'lifecycle', 'architecture', 'workflow'}
            if command == 'check' and len(args.paths) > 1 and args.paths[0] in diagram_types:
                return _main(argv, recorded)

            candidate_sha256 = _candidate_sha256(args.candidate_sha256)
            if command == 'recover':
                if len(args.paths) != 1:
                    raise ArgumentError('recover requires one output.html')
                stage = 'publish'
                from .delivery import recover
                result = recover(args.paths[0])
            elif command in ('check', 'browser-check', 'visual-check'):
                if len(args.paths) != 1:
                    raise ArgumentError(command + ' requires one output.html')
                from .output import canonical_path
                from .delivery import sidecar

                stage = 'input'
                output = canonical_path(args.paths[0])
                if output.is_symlink():
                    raise DiagnosticError('Artifact must not be a symbolic link.',
                        [diagnostic('integrity/unsafe-file', 'Artifact must not be a symbolic link.')])
                native = receipt_path(output)
                saved = _read_receipt(native) if native.exists() or native.is_symlink() else None
                markers = [output.parent / '.archify-delivery-lock.json',
                           sidecar(output, '.delivery-pending.json')]
                if any(marker.exists() or marker.is_symlink() for marker in markers):
                    raise DiagnosticError('Delivery or recovery must finish.',
                        [diagnostic('integrity/delivery-in-progress', 'Delivery or recovery must finish.')])
                if saved is not None and saved.get('deliveryContract') != CONTRACT:
                    raise DiagnosticError('Unsupported delivery receipt.',
                        [diagnostic('integrity/receipt-invalid', 'Unsupported delivery receipt.')])

                stage = 'check'
                if saved and saved.get('command') == 'compare':
                    html = check_comparison_delivery(output, saved)
                    static = {'status': 'passed', 'checks': [{'name': 'comparison_html', 'ok': True}]}
                    kind = 'architecture'
                elif saved:
                    from .integrity import check_delivery
                    _, static, html = check_delivery(saved['input'], output, saved['type'], args.quality)
                    kind = saved['type']
                else:
                    from .integrity import check_html
                    html = read_regular(output)
                    static = check_html(html, args.quality, 'artifact')
                    static['checks'] = [check for check in static['checks']
                                        if check['name'] != 'artifact_layout']
                    kind = None
                    legacy = sidecar(output, '.delivery.json')
                    if legacy.exists() or legacy.is_symlink():
                        saved = _read_receipt(legacy)
                        if (saved.get('schemaVersion') != 1 or saved.get('command') != 'deliver'
                                or saved.get('status') != 'current' or saved.get('output') != str(output)
                                or saved.get('artifact') != identity(html)):
                            raise DiagnosticError('Legacy delivery receipt does not match.',
                                [diagnostic('integrity/receipt-invalid', 'Legacy delivery receipt does not match.')])
                        if identity(read_regular(saved['input'])) != saved.get('specification'):
                            raise DiagnosticError('Legacy candidate differs.',
                                [diagnostic('integrity/specification-mismatch', 'Legacy candidate differs.')])
                        kind = saved.get('type')

                if candidate_sha256:
                    if not saved or not isinstance(saved.get('input'), str):
                        raise ArgumentError('--candidate-sha256 requires a delivery receipt with a known input')
                    candidate = read_regular(saved['input'])
                    if identity(candidate)['sha256'] != candidate_sha256:
                        raise DiagnosticError('Candidate hash differs.',
                            [diagnostic('integrity/specification-mismatch', 'Candidate hash differs.')])
                if args.require_provenance and not saved:
                    raise DiagnosticError('Delivery provenance is required.',
                        [diagnostic('integrity/receipt-required', 'Delivery provenance is required.')])

                result.update(type=kind, output=str(output), artifact=identity(html), staticChecks=static,
                              artifactIntegrity={'status': 'passed'},
                              provenance='byte-bound' if saved else 'unavailable',
                              visualReview='not-requested')
                stage = 'browser'
                policy = args.browser if command == 'check' else 'required'
                if saved and saved.get('command') == 'compare':
                    browser = _comparison_browser_result(html, policy)
                else:
                    from .browser import check_browser
                    browser = check_browser(html, policy)
                result['browserEvidence'] = browser
                if browser['status'] == 'failed' or policy == 'required' and browser['status'] != 'passed':
                    raise DiagnosticError('Browser inspection did not pass.',
                        browser.get('diagnostics') or [
                            diagnostic('browser/unavailable', browser.get('reason', 'Browser unavailable.'))])
                if command == 'visual-check' or args.out_dir and command == 'browser-check':
                    directory = (Path(args.out_dir).absolute() if args.out_dir else
                                 output.parent / (output.stem + '-visual-' + str(uuid.uuid4())[:8]))
                    try:
                        result['captureEvidence'] = capture_artifact(html, directory)
                    except (ImportError, ModuleNotFoundError) as error:
                        raise DiagnosticError(str(error),
                            [diagnostic('browser/unavailable', str(error))]) from error
                    result['visualReview'] = 'automated-capture-only'
                if saved:
                    expected_receipt = (native if saved.get('deliveryContract') == CONTRACT
                                        else sidecar(output, '.delivery.json'))
                    if _read_receipt(expected_receipt) != saved:
                        raise DiagnosticError('Receipt changed during inspection.',
                            [diagnostic('integrity/receipt-changed', 'Receipt changed during inspection.')])
                    for key, identity_key in [('input', 'specification'),
                                              ('baseInput', 'baseSpecification')]:
                        if key in saved and identity(read_regular(saved[key])) != saved.get(identity_key):
                            raise DiagnosticError('Candidate changed during inspection.',
                                [diagnostic('integrity/specification-mismatch',
                                            'Candidate changed during inspection.')])
                if read_regular(output) != html:
                    raise DiagnosticError('Artifact changed during inspection.',
                        [diagnostic('integrity/artifact-mismatch', 'Artifact changed during inspection.')])
                result['ok'] = True
            elif command == 'compare':
                if len(args.paths) not in (3, 4) or args.paths[0] != 'architecture':
                    raise ArgumentError('compare architecture requires base.json head.json [output.html]')
                from .validation import validate_document
                from .repository import verify_repository
                from .comparison import canonical_json, canonical_architecture, compare_architecture, render_comparison
                from .architecture import render_architecture
                from .checks import check_svg
                from .resources import get_resource_root
                from .output import canonical_path, resolve_output, snapshot, publish
                from .delivery import sidecar

                stage = 'input'
                paths = [canonical_path(value) for value in args.paths[1:3]]
                raw = [read_regular(path) for path in paths]
                if candidate_sha256 and identity(raw[1])['sha256'] != candidate_sha256:
                    raise DiagnosticError('Candidate hash differs.',
                        [diagnostic('integrity/specification-mismatch', 'Candidate hash differs.')])
                try:
                    diagrams = [json.loads(content.decode('utf-8'), parse_int=js_integer,
                        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                        for content in raw]
                except (ValueError, UnicodeError) as error:
                    raise DiagnosticError(str(error),
                        [diagnostic('input/json-parse', str(error))]) from error

                stage = 'check'
                evidence = {}
                for name, diagram, content in zip(('base', 'head'), diagrams, raw):
                    validate_document(diagram, 'architecture')
                    verified = verify_repository(diagram, 'architecture', args.repo_root)
                    evidence.update({name + 'RawSha256': identity(content)['sha256'],
                        name + 'Bytes': len(content),
                        name + 'SemanticSha256': hashlib.sha256(
                            canonical_json(canonical_architecture(diagram)).encode()).hexdigest(),
                        name + 'Verified': bool(verified)})

                result = compare_architecture(*diagrams, evidence=evidence)
                result.update(runtime='python', deliveryContract=CONTRACT, input=str(paths[1]),
                              baseInput=str(paths[0]), specification=identity(raw[1]),
                              baseSpecification=identity(raw[0]), receiptId=str(uuid.uuid4()))
                requested = (args.paths[3] if len(args.paths) == 4 else
                             str(Path(args.out_dir or '.') / 'architecture-delta.html'))
                output = resolve_output(requested, None, paths[1], 'architecture-delta.html')
                if output == paths[0]:
                    raise ArgumentError('output must not replace base input')
                result['output'] = str(output)

                stage = 'render'
                expected = snapshot(output)
                svgs = [render_architecture(diagram, quality=args.quality) for diagram in diagrams]
                for svg in svgs:
                    check_svg(svg, args.quality, 'architecture')
                template = (get_resource_root() / 'assets/template.html').read_text(encoding='utf-8')
                html = render_comparison(result, *svgs, template)

                stage = 'check'
                check_comparison_html(html)
                result['visualReview'] = 'not-requested'
                result.update(artifact=identity(html),
                    staticChecks={'status': 'passed', 'checks': [{'name': 'comparison_html', 'ok': True}]},
                    requestedReceipt=(str(canonical_path(args.receipt)) if args.receipt
                                      else str(sidecar(output, '.receipt.json'))),
                    diagnostics=list(recorded))
                if Path(result['requestedReceipt']) in paths:
                    raise ArgumentError('receipt must not replace either input')

                stage = 'browser'
                browser = _comparison_browser_result(html, args.browser)
                result['browserEvidence'] = browser
                browser_ok = browser['status'] != 'failed' and (
                    args.browser != 'required' or browser['status'] == 'passed')
                if not browser_ok:
                    result.update(ok=False, stage='browser',
                        diagnostics=_merge_diagnostics(recorded, browser.get('diagnostics') or [
                            diagnostic('browser/unavailable',
                                       browser.get('reason', 'Browser unavailable.'))]))
                else:
                    result.update(ok=True, diagnostics=_merge_diagnostics(recorded))

                def verifier():
                    if read_regular(paths[0]) != raw[0]:
                        raise DiagnosticError('Base candidate changed.',
                            [diagnostic('integrity/specification-mismatch', 'Base candidate changed.')])
                    return verify_bytes(paths[1], output, raw[1], html)

                stage = 'publish'
                publish(output, html, expected, 'compare', result['receiptId'],
                        receipt=result, verifier=verifier)
                if not browser_ok:
                    stage = 'browser'
                    raise DiagnosticError('Browser inspection did not pass; static HTML was generated.',
                        browser.get('diagnostics') or [
                            diagnostic('browser/unavailable',
                                       browser.get('reason', 'Browser unavailable.'))])
                if args.open:
                    import webbrowser
                    webbrowser.open(output.as_uri())
            else:
                raise ArgumentError('unsupported command')
        except ArgumentError as error:
            result.update(ok=False, stage='arguments', exitCode=2, error=str(error),
                diagnostics=_merge_diagnostics(recorded, result.get('diagnostics', []),
                    [diagnostic('cli/invalid-arguments', str(error))]))
        except DiagnosticError as error:
            result.update(ok=False, stage=stage, exitCode=1, error=str(error),
                diagnostics=_merge_diagnostics(recorded, result.get('diagnostics', []), error.diagnostics))
        except ImportError as error:
            result.update(ok=False, stage=stage, exitCode=1, error=str(error),
                diagnostics=_merge_diagnostics(recorded, result.get('diagnostics', []),
                    [diagnostic('python/dependency-missing', str(error))]))
        except OSError as error:
            result.update(ok=False, stage=stage, exitCode=1, error=str(error),
                diagnostics=_merge_diagnostics(recorded, result.get('diagnostics', []),
                    [diagnostic('command/failed', str(error))]))
        except Exception as error:
            result.update(ok=False, stage=stage, exitCode=1, error=str(error),
                diagnostics=_merge_diagnostics(recorded, result.get('diagnostics', []),
                    [diagnostic('command/failed', str(error))]))

        if result.get('ok'):
            result['diagnostics'] = _merge_diagnostics(recorded, result.get('diagnostics', []))
            result.setdefault('stage', stage)

    print(json.dumps(result, ensure_ascii=True, indent=2) if json_mode else
          str(result.get('output', result.get('status'))) if result['ok'] else result['error'],
          file=sys.stdout if json_mode or result['ok'] else sys.stderr)
    return 0 if result['ok'] else result.get('exitCode',1)


def _merge_diagnostics(*groups):
    merged = []
    for group in groups:
        for entry in group or []:
            if entry not in merged:
                merged.append(entry)
    return merged


def _candidate_sha256(value):
    if value is None:
        return None
    from .cli import ArgumentError
    if not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ArgumentError('--candidate-sha256 requires 64 hex digits')
    return value.lower()


def _read_receipt(path):
    try:
        receipt = json.loads(read_regular(path))
    except (ValueError, UnicodeError) as error:
        message = 'Invalid delivery receipt.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-invalid', message,
            {'path': str(path)}, {'reason': str(error)})]) from error
    if not isinstance(receipt, dict):
        message = 'Invalid delivery receipt.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-invalid', message,
            {'path': str(path)})])
    return receipt


def _comparison_browser_result(html, policy):
    try:
        return check_comparison_browser(html, policy)
    except Exception as error:
        return {'policy': policy, 'status': 'failed', 'reason': 'browser-runtime-error',
                'evidenceKind': 'none',
                'diagnostics': [diagnostic('browser/runtime-error', str(error))]}


def check_comparison_html(html):
    import xml.etree.ElementTree as ET

    try:
        text = html.decode('utf-8')
    except UnicodeError as error:
        message = 'Comparison HTML must be UTF-8.'
        raise DiagnosticError(message, [diagnostic('artifact/html-encoding', message)]) from error
    fragments = re.findall(r'<(?:\w+:)?svg\b[\s\S]*?</(?:\w+:)?svg>', text)
    if len(fragments) != 3 or 'archify-compare-receipt' not in text:
        message = 'Comparison needs three complete SVG views and its receipt.'
        raise DiagnosticError(message, [diagnostic('artifact/comparison-structure', message)])
    ids = []
    for fragment in fragments:
        try:
            root = ET.fromstring(fragment)
        except ET.ParseError as error:
            message = 'Comparison contains invalid SVG XML.'
            raise DiagnosticError(message, [diagnostic('artifact/svg-xml', message,
                evidence={'reason': str(error)})]) from error
        ids.extend(element.get('id') for element in root.iter() if element.get('id'))
        try:
            box = [float(value) for value in root.get('viewBox', '').split()]
        except ValueError:
            box = []
        import math
        if len(box) != 4 or not all(math.isfinite(value) for value in box) or min(box[2:]) <= 0:
            message = 'Comparison SVG needs a finite viewBox with positive width and height.'
            raise DiagnosticError(message, [diagnostic('artifact/viewbox', message)])
    if len(ids) != len(set(ids)):
        message = 'Comparison contains duplicate SVG IDs.'
        raise DiagnosticError(message, [diagnostic('artifact/duplicate-id', message)])
    known = set(ids)
    for fragment in fragments:
        for element in ET.fromstring(fragment).iter():
            for key, value in element.attrib.items():
                references = re.findall(r'url\(#([^)]*)\)', value)
                if key.endswith('href') and value.startswith('#'):
                    references.append(value[1:])
                if any(reference not in known for reference in references):
                    message = 'Comparison SVG references a missing ID.'
                    raise DiagnosticError(message, [diagnostic('artifact/missing-reference', message)])


def check_comparison_delivery(output,saved):
    from .delivery import sidecar
    markers = [output.parent / '.archify-delivery-lock.json',
               sidecar(output, '.delivery-pending.json')]
    if any(marker.exists() or marker.is_symlink() for marker in markers):
        message = 'Delivery or recovery is in progress.'
        raise DiagnosticError(message, [diagnostic('integrity/delivery-in-progress', message)])
    required = ('input', 'baseInput', 'specification', 'baseSpecification', 'artifact', 'output')
    if any(key not in saved for key in required):
        message = 'Comparison receipt is missing required fields.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-invalid', message)])
    before = read_regular(receipt_path(output))
    html = read_regular(output)
    try:
        current_receipt = json.loads(before)
    except (ValueError, UnicodeError) as error:
        message = 'Comparison receipt became invalid during inspection.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-invalid', message,
            evidence={'reason': str(error)})]) from error
    if current_receipt != saved:
        message = 'Comparison receipt changed during inspection.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-changed', message)])
    if saved.get('output') != str(output):
        message = 'Comparison receipt names a different output.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-invalid', message)])
    if saved.get('artifact') != identity(html):
        message = 'Comparison artifact no longer matches its receipt.'
        raise DiagnosticError(message, [diagnostic('integrity/artifact-mismatch', message)])
    candidates = []
    for field, identity_field in [('input', 'specification'), ('baseInput', 'baseSpecification')]:
        content = read_regular(saved[field])
        candidates.append((saved[field], content))
        if identity(content) != saved[identity_field]:
            message = 'Comparison candidate no longer matches its receipt.'
            raise DiagnosticError(message, [diagnostic('integrity/specification-mismatch', message)])
    check_comparison_html(html)
    if before != read_regular(receipt_path(output)):
        message = 'Receipt changed during comparison inspection.'
        raise DiagnosticError(message, [diagnostic('integrity/receipt-changed', message)])
    if html != read_regular(output):
        message = 'Comparison artifact changed during inspection.'
        raise DiagnosticError(message, [diagnostic('integrity/artifact-mismatch', message)])
    if any(read_regular(path) != content for path, content in candidates):
        message = 'Comparison candidate changed during inspection.'
        raise DiagnosticError(message, [diagnostic('integrity/specification-mismatch', message)])
    return html


def check_comparison_browser(html,policy):
    result = {'policy': policy, 'status': 'skipped', 'diagnostics': [],
              'reason': 'disabled-by-policy', 'evidenceKind': 'none'}
    if policy == 'off':
        return result
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError) as error:
        result['reason'] = 'playwright-unavailable: ' + str(error)
        return result

    try:
        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
            if not executable.is_file():
                result['reason'] = 'chromium-unavailable: executable missing at ' + str(executable)
                return result
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.route('http://**/*', lambda route: route.abort())
                page.route('https://**/*', lambda route: route.abort())
                page.set_content(html.decode('utf-8'), wait_until='load')
                for name in ('base', 'delta', 'head'):
                    page.locator('[data-tab="' + name + '"]').click()
                    if not page.locator('[data-view="' + name + '"] svg').is_visible():
                        message = 'Comparison view ' + name + ' is unavailable.'
                        result.update(status='failed', reason='comparison-view-unavailable',
                            diagnostics=[diagnostic('browser/comparison-view-unavailable', message)])
                        return result
            finally:
                browser.close()
            if errors:
                result.update(status='failed', reason='browser-page-error',
                    diagnostics=[diagnostic('browser/page-error', 'Uncaught browser page error.',
                                            evidence={'errors': errors})])
                return result
        result.update(status='passed', reason=None, evidenceKind='automated-browser',
                      artifact=identity(html))
        return result
    except Exception as error:
        result.update(status='failed', reason='browser-runtime-error',
            diagnostics=[diagnostic('browser/runtime-error', str(error))])
        return result


def capture_artifact(html,directory):
    """Capture immutable inspected bytes; capture is not perceptual approval."""
    from playwright.sync_api import sync_playwright
    directory.mkdir(parents=True,exist_ok=False)
    records=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        try:
            page=browser.new_page(viewport={'width':1440,'height':900})
            page.route('http://**/*',lambda route:route.abort());page.route('https://**/*',lambda route:route.abort())
            page.set_content(html.decode('utf-8'),wait_until='load')
            page.evaluate('document.fonts.ready')
            for theme in ['light','dark']:
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                image=page.screenshot(full_page=True)
                target=directory/(theme+'.png')
                with target.open('xb') as stream:stream.write(image)
                records.append({'path':str(target),'theme':theme,**identity(image)})
        finally:browser.close()
    return {'status':'captured','evidenceKind':'automated-capture','artifact':identity(html),'screenshots':records,'perceptualApproval':False}
