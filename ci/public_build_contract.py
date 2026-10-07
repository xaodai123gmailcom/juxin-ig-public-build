"""Pure source contracts for the public workflow's original Windows build.

These checks read source only. They never import the application, start a build,
install dependencies, or change telemetry consent.
"""
import ast
import re


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def function(source, name):
    matches = [node for node in ast.parse(source).body
               if isinstance(node, ast.FunctionDef) and node.name == name]
    require(len(matches) == 1, 'Expected one ' + name + ' function')
    return matches[0]


def direct_calls(node, name):
    return [statement.value for statement in node.body
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name) and statement.value.func.id == name]


def gate_call(node, label):
    matches = [call for call in direct_calls(node, 'run_owned')
               if call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == label]
    require(len(matches) == 1, 'Missing or conditional required gate: ' + label)
    return matches[0]


def assert_public_build_chain(workflow, wrapper, common):
    # This workflow deliberately uses simple one-line cmd steps. Reject an
    # optional/conditional replacement, including commented-out commands.
    require('continue-on-error' not in workflow, 'Workflow must fail closed')
    guard = re.search(r'^  verify:\n    if: >-\n(.*?)^    runs-on: windows-2022$', workflow, re.M | re.S)
    require(guard is not None and ' '.join(guard.group(1).split()) ==
            "github.repository == 'xaodai123gmailcom/juxin-ig-public-build' && "
            "github.repository_id == '1406784621' && "
            "github.repository_owner_id == '337452708' && "
            "github.event.repository.private == false", 'Build job must use only its exact public repository guard')
    steps = re.findall(r'^      - [\s\S]*?(?=^      - |\Z)', workflow, re.M)
    positions = []
    for stage in ('contracts', 'early', 'build', 'installed'):
        command = 'python -I -X utf8 ci/public_ci.py ' + stage
        matching = [(index, step) for index, step in enumerate(steps)
                    if re.search(r'^        run: ' + re.escape(command) + r'$', step, re.M)]
        require(len(matching) == 1, 'Missing exact mandatory workflow stage: ' + stage)
        index, step = matching[0]
        require(not re.search(r'^        if:', step, re.M), 'Optional workflow stage: ' + stage)
        require(re.search(r'^        working-directory: source$', step, re.M), 'Wrong stage checkout')
        require(re.search(r'^        shell: cmd$', step, re.M), 'Wrong stage shell')
        positions.append(index)
    require(positions == sorted(set(positions)), 'Required workflow stage order changed')

    shell = function(wrapper, 'powershell')
    expected_shell = ast.parse("return [str(Path(os.environ['SystemRoot']) / "
        "'System32/WindowsPowerShell/v1.0/powershell.exe'), '-NoLogo', '-NoProfile', "
        "'-NonInteractive', '-File', str(ROOT / script)]").body[0]
    require(len(shell.body) == 1 and ast.dump(shell.body[0], include_attributes=False)
            == ast.dump(expected_shell, include_attributes=False),
            'Wrapper must execute the requested original PowerShell script')

    build = function(wrapper, 'build')
    call = gate_call(build, 'full-original-windows-build')
    require(ast.dump(call.args[1], include_attributes=False) == ast.dump(ast.parse(
        "powershell('scripts/build_windows.ps1') + ['-BrowserMode', 'installed-chrome']",
        mode='eval').body, include_attributes=False), 'Public build must invoke the original full installed-Chrome build')
    require(len(call.args) == 3 and ast.literal_eval(call.args[2]) == 10800 and not call.keywords,
            'Original build command or deadline changed')
    require(len(build.body) >= 2 and isinstance(build.body[-1], ast.Return)
            and ast.unparse(build.body[-1].value) == 'validate_source_build(state)',
            'Build must validate original output after the required command')
    require(not any(isinstance(node, (ast.Try, ast.If, ast.Return)) for node in build.body[:-1]),
            'Build cannot skip or swallow its required gate')
    require(call.lineno < build.body[-1].lineno, 'Output validation must follow the build')
    guard = "require(read_json(state_root() / 'early-result.json')['status'] == 'passed', 'Early gates did not pass')"
    require(any(ast.dump(statement, include_attributes=False) == ast.dump(ast.parse(guard).body[0], include_attributes=False)
                and statement.lineno < call.lineno for statement in build.body),
            'Build must reject unpassed early gates')

    owner = function(common, 'run_owned')
    required = "runner.terminal_receipt(receipt) and receipt['targetExitCode'] == 0 and receipt['outcome'] == 'completed'"
    checks = [check for check in direct_calls(owner, 'require')
              if check.args and ast.dump(check.args[0], include_attributes=False)
              == ast.dump(ast.parse(required, mode='eval').body, include_attributes=False)]
    require(len(checks) == 1 and checks[0].lineno < owner.body[-1].lineno,
            'Owned build must reject nonzero, nonterminal, or incomplete results')


def powershell_depth(source, offset):
    # Count real braces, excluding comments and quoted strings. The audited
    # build has no here-strings or block comments; fail closed if introduced.
    require(not re.search(r'(?m)^\s*[@][\"\x27]|<#', source), 'Review new PowerShell quoting before accepting it')
    tokens = re.finditer(r'"(?:`.|[^"`])*"|\x27(?:\x27\x27|[^\x27])*\x27|#[^\n]*|[{}]', source[:offset])
    return sum(1 if token.group() == '{' else -1 if token.group() == '}' else 0 for token in tokens)


def assert_recovery_gate(build):
    # Keep the local gate: an actual isolated discovery command, immediately
    # checked, in the outer build try body, before PyInstaller can execute.
    gate = re.search(
        r'^\$BuildRecoveryExitCode = Invoke-IgacNativeCommandWithLog `\n'
        r'    -FilePath \(Join-Path \$Root "\.venv\\Scripts\\python\.exe"\) `\n'
        r'    -ArgumentList @\("-I", "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts\\tests", "-p", "test_build_recovery_r94\.py", "-v"\) `\n'
        r'    -LogPath \(Join-Path \$InstallerOutput "build-recovery-r94-full\.log"\)\n'
        r'if \(\$BuildRecoveryExitCode -ne 0\) \{\n    throw "[^"\n]+"\n\}', build, re.M)
    require(gate is not None, 'Recovery discovery and immediate throwing failure check are mandatory')
    require(build.count('$BuildRecoveryExitCode =') == 1, 'Recovery result cannot be overwritten')
    freeze = re.search(r'^\$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog', build, re.M)
    require(freeze is not None and gate.end() < freeze.start(), 'Recovery gate must precede PyInstaller')
    require(powershell_depth(build, gate.start()) == 1 and powershell_depth(build, freeze.start()) == 1,
            'Recovery and freeze must run unconditionally in the outer build body')


def assert_ui_dependencies(wrapper, build, install):
    early = function(wrapper, 'early')
    npm = gate_call(early, 'early-node-dependencies')
    require(ast.literal_eval(npm.args[1]) == ['npm.cmd', 'ci', '--include=dev', '--no-audit', '--no-fund'],
            'Early UI fixtures require installed development dependencies')
    for label in ('public-cloud-configuration', 'early-all-required-regressions'):
        require(npm.lineno < gate_call(early, label).lineno, 'Install UI dependencies before backend fixtures')
    install_call = '& "$PSScriptRoot\\install_windows.ps1"'
    require(build.index(install_call) < build.index('scripts\\run_backend_tests.py'),
            'Local build must install UI dependencies before backend tests')
    require('npm ci --include=dev --include=optional --ignore-scripts=false --bin-links=true' in install,
            'Local npm install must retain UI fixtures and runtime install scripts')
    require('Assert-LastExitCode "Install desktop APP dependencies"' in install,
            'Dependency installation must fail closed')


def assert_runtime_imports(build):
    # A required argument written only in a comment is not a packaging option.
    build = re.sub(r'"(?:`.|[^"`])*"|\x27(?:\x27\x27|[^\x27])*\x27|#[^\n]*',
                   lambda match: ' ' * len(match.group()) if match.group().startswith('#') else match.group(), build)
    arguments = re.search(r'^\$PyInstallerArguments = @\((.*?)^\)', build, re.M | re.S)
    require(arguments is not None, 'Original PyInstaller arguments are missing')
    for option in ('"--collect-all", "playwright"', '"--hidden-import", "websockets.sync.client"'):
        require(option in arguments.group(1), 'Frozen runtime import missing: ' + option)
    require(build.count('$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog') == 1,
            'Expected one original freeze shared by installer and portable packaging')
    require('-ArgumentList $PyInstallerArguments' in build and 'if ($PyInstallerExitCode -ne 0)' in build,
            'The required freeze must consume and check its runtime arguments')


def assert_browser_prerequisites(early, build):
    tree = ast.parse(early)
    selection = gate_call(tree, 'early-browser-selection')
    require('scripts/browser_build_policy.py' in ast.unparse(selection), 'Select actual installed Chrome before browser tests')
    prechecks = [gate_call(tree, label) for label in ('early-saturation-focused-precheck',)]
    require(all(selection.lineno < check.lineno for check in prechecks), 'Browser selection must precede early backend gates')
    require("IGAC_TEST_CHROMIUM_EXECUTABLE=chrome" in early and "IGAC_REQUIRE_COLLECTION_BROWSER='1'" in early,
            'Early browser gates must receive selected installed Chrome')
    require(build.index('scripts\\browser_build_policy.py --candidate-output $EarlyChromeCandidatePath')
            < build.index('    Invoke-IgacInstagramThousandGate'), 'Select Chrome before early real-browser local gate')
    require('Assert-LastExitCode "Find installed Chrome for early collection regression"' in build,
            'Early installed Chrome selection must fail closed')
    prepare = build.index('$env:PLAYWRIGHT_BROWSERS_PATH = $BrowserRuntime')
    native = build.index('scripts\\run_backend_tests.py -p "test_native_launch.py"')
    for token in ('scripts\\browser_build_policy.py --candidate-output $ChromeCandidatePath',
                  'Assert-LastExitCode "Find the required installed Chrome"',
                  'playwright install chromium --no-shell', 'scripts\\prune_browser_runtime.py'):
        require(prepare < build.index(token) < native, 'Browser mode preparation must precede native backend tests: ' + token)


def same_expression(actual, expected):
    return ast.dump(actual, include_attributes=False) == ast.dump(ast.parse(expected, mode='eval').body, include_attributes=False)


def require_expression(node, expected, message):
    require(any(call.args and same_expression(call.args[0], expected) for call in direct_calls(node, 'require')), message)


def assert_native_logging_wiring(wrapper, build, pwsh):
    call = gate_call(function(wrapper, 'contracts'), 'powershell7-native-command-logging')
    require(same_expression(call.args[1], "['pwsh.exe', '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(ROOT / 'ci/public_ci_pwsh_logging.ps1')]"),
            'Public contracts must execute the real PowerShell 7 logging probe')
    for token in ("if ($PSVersionTable.PSVersion.Major -lt 7) { throw", "$PSNativeCommandUseErrorActionPreference = $true",
                  "& (Join-Path $PSScriptRoot '..\\scripts\\test_native_command_logging.ps1')",
                  'if (-not $PSNativeCommandUseErrorActionPreference) {\n    throw'):
        require(token in pwsh, 'PowerShell 7 native logging/restoration check is missing: ' + token)
    require(pwsh.index('$PSNativeCommandUseErrorActionPreference = $true') < pwsh.index('& (Join-Path')
            < pwsh.index('if (-not $PSNativeCommandUseErrorActionPreference)'), 'PowerShell 7 preference probe order changed')
    require(re.search(r'^& \(Join-Path \$PSScriptRoot ', pwsh, re.M) is not None
            and powershell_depth(pwsh, pwsh.index('& (Join-Path')) == 0,
            'PowerShell 7 logging probe cannot be commented out or conditional')
    probe = '& "$PSScriptRoot\\test_native_command_logging.ps1"'
    require(build.index(probe) < build.index('& "$PSScriptRoot\\install_windows.ps1"')
            < build.index('$PyInstallerExitCode = Invoke-IgacNativeCommandWithLog'),
            'The actual original Windows PowerShell host must check safe logging before building')
    require(powershell_depth(build, build.index(probe)) == 1, 'Original logging probe cannot be optional')


def assert_unicode_runtime_wiring(wrapper, unicode, verifier):
    early, build = function(wrapper, 'early'), function(wrapper, 'build')
    source_call = gate_call(early, 'unicode-source-runtime')
    frozen_call = gate_call(build, 'unicode-copied-frozen-runtime')
    for call, mode in ((source_call, 'source'), (frozen_call, 'frozen')):
        require(same_expression(call.args[1], "[sys.executable, '-I', '-X', 'utf8', str(ROOT / 'ci/public_ci_unicode.py'), '" + mode + "']"),
                'Required Unicode entry must execute its real helper: ' + mode)
    require(gate_call(early, 'early-python-dependencies').lineno < source_call.lineno
            < gate_call(early, 'early-node-dependencies').lineno, 'Unicode source verification must fail before long suites')
    validations = direct_calls(build, 'validate_source_build')
    require(len(validations) == 1 and gate_call(build, 'full-original-windows-build').lineno
            < validations[0].lineno < frozen_call.lineno < build.body[-1].lineno,
            'Copied frozen verification must follow the validated original build and precede final validation')

    source = function(unicode, 'source_gates')
    guarded = [node for node in source.body if isinstance(node, ast.Try)]
    require(len(guarded) == 1 and not guarded[0].handlers and not guarded[0].orelse,
            'Unicode source failures must propagate through cleanup')
    body = ast.Module(body=guarded[0].body, type_ignores=[])
    for label in ('unicode-source-create-venv', 'unicode-source-pinned-dependencies',
                  'unicode-source-pinned-models', 'unicode-source-model-integrity'):
        gate_call(body, label)
    modes = [node for node in body.body if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == 'mode']
    require(len(modes) == 1 and ast.literal_eval(modes[0].iter) == ('source-probe', 'classifier-probe'),
            'Real source inference and actual classifier must both execute')
    calls = direct_calls(modes[0], 'run_owned')
    require(len(calls) == 1 and same_expression(calls[0].args[1], 'python_command(python, Path(__file__), mode, identifier)')
            and same_expression(calls[0].args[3], 'environment'), 'Unicode probe loop must execute both children with strict settings')
    settings = next((node.value for node in body.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'environment' for target in node.targets)), None)
    require(settings is not None and same_expression(settings,
            "{'IGAC_TEST_REQUIRE_NON_ASCII_OPENVINO_SOURCE': '1', 'IGAC_OPENVINO_CACHE_ROOT': str(cache)}"),
            'Real Unicode probes must require a Unicode DLL and their fresh cache')
    context = function(unicode, 'source_context')
    require_expression(context, "distribution.version == '2025.4.1' and not str(original_dll).isascii() and original_dll.is_relative_to((source / '.venv').resolve())",
                       'Source probe must reject ASCII or unrelated original native DLLs')
    probe = function(unicode, 'source_probe')
    require(any(same_expression(statement.value, "verifier['verify_source'](work / 'unicode-native-manifest.json')")
                for statement in probe.body if isinstance(statement, ast.Expr)), 'Real source model inference must execute')
    require(direct_calls(probe, 'assert_cache'), 'Source inference must verify the fresh ASCII cache')
    classifier = function(unicode, 'classifier_probe')
    unpack = [node for node in classifier.body if isinstance(node, ast.Assign)
              and same_expression(node.value, 'classifier._get_compiled_models()')]
    require(len(unpack) == 1 and len(unpack[0].targets) == 1 and isinstance(unpack[0].targets[0], ast.Tuple)
            and len(unpack[0].targets[0].elts) == 4, 'Actual Unicode classifier must unpack all four compiled models')
    require_expression(classifier, "result.checked and result.reason not in {'model_unavailable', 'model_integrity_failed', 'local_inference_failed'}",
                       'Actual Unicode classifier result must reject failed inference')
    require(any(isinstance(node, ast.Assign) and same_expression(node.value, 'exercise_local_openvino_gender_branch(gender)')
                for node in classifier.body), 'Actual production gender parser must execute')
    require(direct_calls(classifier, 'assert_cache'), 'Actual classifier must verify the fresh ASCII cache')
    models = function(verifier, '_run_real_inference')
    loops = [node for node in models.body if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and node.target.id == 'filename']
    require(len(loops) == 1 and ast.literal_eval(loops[0].iter) == (
        'face-detection-retail-0004.xml', 'age-gender-recognition-retail-0013.xml',
        'person-detection-retail-0013.xml', 'person-attributes-recognition-crossroad-0230.xml'),
        'Real source verifier must retain all four model inferences')

    frozen = function(unicode, 'frozen_gates')
    guarded = [node for node in frozen.body if isinstance(node, ast.Try)]
    require(len(guarded) == 1 and not guarded[0].handlers and not guarded[0].orelse,
            'Copied frozen failures must propagate through cleanup')
    body = ast.Module(body=guarded[0].body, type_ignores=[])
    calls = [gate_call(body, label) for label in ('unicode-frozen-original-layout', 'unicode-frozen-copied-layout',
             'unicode-frozen-real-inference', 'unicode-frozen-real-service')]
    require([call.lineno for call in calls] == sorted(call.lineno for call in calls), 'Copied frozen layout/inference/service order changed')
    require(same_expression(calls[2].args[1], "[str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(ROOT / 'scripts/test_frozen_openvino.ps1'), '-Executable', str(copied / 'collector_core.exe'), '-LogPath', str(work / 'frozen-openvino.log'), '-TimeoutSeconds', '180', '-RequireNonAsciiSource', '-ExpectedCacheRoot', str(cache)]"),
            'Copied frozen inference must require Unicode source and a fresh ASCII cache')
    require_expression(body, 'fingerprint(original) == fingerprint(copied) == sealed', 'Frozen Unicode copy must preserve exact built bytes')


def read_sources(root):
    return {name: (root / path).read_text(encoding='utf-8-sig') for name, path in {
        'workflow': '.github/workflows/public-windows-verify.yml',
        'wrapper': 'ci/public_ci.py', 'common': 'ci/public_ci_common.py',
        'build': 'scripts/build_windows.ps1', 'install': 'scripts/install_windows.ps1',
        'early': 'ci/public_ci_early.py',
        'unicode': 'ci/public_ci_unicode.py', 'pwsh': 'ci/public_ci_pwsh_logging.ps1',
        'verifier': 'scripts/verify_openvino_windows.py',
    }.items()}
