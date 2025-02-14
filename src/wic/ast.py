import copy
from pathlib import Path
import re
import sys
import traceback
from typing import Dict, List, Tuple

from mergedeep import merge, Strategy
from jsonschema import Draft202012Validator
import yaml

from . import utils
from .wic_types import Namespaces, Yaml, Tools, YamlTree, YamlForest, StepId, NodeData, RoseTree

# NOTE: AST = Abstract Syntax Tree

# TODO: Check for inline-ing subworkflows more than once and, if there are not
# any modifications from any parent dsl args, use yaml anchors and aliases.
# That way, we should be able to serialize back to disk without duplication.


def read_ast_from_disk(homedir: str,
                       yaml_tree_tuple: YamlTree,
                       yml_paths: Dict[str, Dict[str, Path]],
                       tools: Tools,
                       validator: Draft202012Validator,
                       ignore_validation_errors: bool) -> YamlTree:
    """Reads the yml workflow definition files from disk (recursively) and inlines them into an AST

    Args:
        homedir (str): The users home directory
        yaml_tree_tuple (YamlTree): A tuple of a filepath and its Yaml file contents.
        yml_paths (Dict[str, Dict[str, Path]]): The yml workflow definitions found using get_yml_paths()
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()
        validator (Draft202012Validator): Used to validate the yml files against the autogenerated schema.
        ignore_validation_errors (bool): Temporarily ignore validation errors. Do not use this permanently!

    Raises:
        Exception: If the yml file(s) do not exist

    Returns:
        YamlTree: A tuple of the root filepath and the associated yml AST
    """
    (step_id, yaml_tree) = yaml_tree_tuple

    try:
        if not ignore_validation_errors:
            validator.validate(yaml_tree)
    except Exception as e:
        yaml_path = Path(step_id.stem)
        print('Failed to validate', yaml_path)
        print(f'See validation_{yaml_path.stem}.txt for detailed technical information.')
        # Do not display a nasty stack trace to the user; hide it in a file.
        with open(f'validation_{yaml_path.stem}.txt', mode='w', encoding='utf-8') as f:
            # https://mypy.readthedocs.io/en/stable/common_issues.html#python-version-and-system-platform-checks
            if sys.version_info >= (3, 10):
                traceback.print_exception(type(e), value=e, tb=None, file=f)
            else:
                traceback.print_exception(etype=type(e), value=e, tb=None, file=f)
        sys.exit(1)

    wic = {'wic': yaml_tree.get('wic', {})}
    if 'backends' in wic['wic']:
        # Recursively expand each backend, but do NOT choose a specific backend.
        # Require back_name to be .yml? For now, yes.
        backends_trees = []
        for back_name, back in wic['wic']['backends'].items():
            plugin_ns = wic['wic'].get('namespace', 'global')
            stepid = StepId(back_name, plugin_ns)
            backends_tree = read_ast_from_disk(homedir, YamlTree(stepid, back), yml_paths, tools, validator,
                                               ignore_validation_errors)
            backends_trees.append(backends_tree)
        yaml_tree['wic']['backends'] = dict(backends_trees)
        return YamlTree(step_id, yaml_tree)

    steps: List[Yaml] = yaml_tree['steps']
    wic_steps = wic['wic'].get('steps', {})
    steps_keys = utils.get_steps_keys(steps)
    tools_stems = [stepid.stem for stepid in tools]
    subkeys = utils.get_subkeys(steps_keys, tools_stems)

    for i, step_key in enumerate(steps_keys):
        stem = Path(step_key).stem

        # Recursively read subworkflows, adding yml file contents
        if step_key in subkeys:
            # Check for namespaceing; otherwise use the namespace 'global'.
            # NOTE: For now, do not support overloading / parameter passing for
            # namespaces, because we would have to call merge_yml_trees here.
            # It could (easily?) be done, but right now we have excellent
            # separation of concerns between simply reading yml files from disk
            # and then performing AST transformations in-memory.
            sub_wic = wic_steps.get(f'({i+1}, {step_key})', {})
            plugin_ns = sub_wic.get('wic', {}).get('namespace', 'global')

            paths_ns_i = yml_paths.get(plugin_ns, {})
            if paths_ns_i == {}:
                wicdir = Path(homedir) / 'wic'
                raise Exception(
                    f'Error! namespace {plugin_ns} not found in yaml paths. Check {wicdir / "yml_dirs.txt"}')
            if stem not in paths_ns_i:
                msg = f'Error! {stem} not found in namespace {plugin_ns} when attempting to read {step_id.stem}.yml'
                if stem == 'in':
                    msg += f'\n(Check that you have properly indented the `in` tag in {step_id.stem})'
                raise Exception(msg)
            yaml_path = paths_ns_i[stem]

            if not (yaml_path.exists() and yaml_path.suffix == '.yml'):
                raise Exception(f'Error! {yaml_path} does not exist or is not a .yml file.')

            # Load the high-level yaml sub workflow file.
            with open(yaml_path, mode='r', encoding='utf-8') as y:
                sub_yaml_tree_raw: Yaml = yaml.safe_load(y.read())

            y_t = YamlTree(StepId(step_key, plugin_ns), sub_yaml_tree_raw)
            (step_id_, sub_yml_tree) = read_ast_from_disk(homedir, y_t, yml_paths, tools, validator,
                                                          ignore_validation_errors)

            step_i_dict = {} if steps[i][step_key] is None else steps[i][step_key]
            # Do not merge these two dicts; use subtree and parentargs so we can
            # apply subtree before compilation and parentargs after compilation.
            steps[i][step_key] = {'subtree': sub_yml_tree, 'parentargs': step_i_dict}

    return YamlTree(step_id, yaml_tree)


def merge_yml_trees(yaml_tree_tuple: YamlTree,
                    wic_parent: Yaml,
                    tools: Tools) -> YamlTree:
    """Implements 'parameter passing' by recursively merging wic: yml tags.
    Values from the parent workflow will overwrite / override subworkflows.
    See https://github.com/PolusAI/mm-workflows/blob/main/examples/gromacs/basic.yml for details

    Args:
        yaml_tree_tuple (YamlTree): A tuple of a name and a yml AST
        wic_parent (Yaml): The wic: yml dict from the parent workflow
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()

    Raises:
        Exception: If a wic: tag is found as an argument to a CWL CommandLineTool

    Returns:
        YamlTree: The yml AST with all wic: tags recursively merged.
    """
    (step_id, yaml_tree) = yaml_tree_tuple

    # Check for top-level yml dsl args
    wic_self = {'wic': yaml_tree.get('wic', {})}
    wic = merge(wic_self, wic_parent, strategy=Strategy.TYPESAFE_REPLACE)
    # Here we want to ADD wic: as a top-level yaml tag.
    # In the compilation phase, we want to remove it.
    yaml_tree['wic'] = wic['wic']
    wic_steps = wic['wic'].get('steps', {})

    if 'backends' in wic['wic']:
        # Recursively expand each backend, but do NOT choose a specific backend.
        # Require back_name to be .yml? For now, yes.
        backends_trees = []
        for stepid, back in wic['wic']['backends'].items():
            backends_tree = merge_yml_trees(YamlTree(stepid, back), wic_parent, tools)
            backends_trees.append(backends_tree)
        yaml_tree['wic']['backends'] = dict(backends_trees)
        return YamlTree(step_id, yaml_tree)

    steps: List[Yaml] = yaml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)
    tools_stems = [stepid.stem for stepid in tools]
    subkeys = utils.get_subkeys(steps_keys, tools_stems)

    for i, step_key in enumerate(steps_keys):
        # Recursively merge subworkflows, to implement parameter passing.
        if step_key in subkeys:
            # Extract the sub yaml file that we pre-loaded from disk.
            sub_yml_tree_initial = steps[i][step_key]['subtree']
            sub_wic = wic_steps.get(f'({i+1}, {step_key})', {})

            y_t = YamlTree(StepId(step_key, step_id.plugin_ns), sub_yml_tree_initial)
            (step_key_, sub_yml_tree) = merge_yml_trees(y_t, sub_wic, tools)
            # Now mutably overwrite the self args with the merged args
            steps[i][step_key]['subtree'] = sub_yml_tree

        # Extract provided CWL args, if any, and (recursively) merge them with
        # provided CWL args passed in from the parent, if any.
        # (At this point, any DSL args provided from the parent(s) should have
        # all of the initial yml tags removed, leaving only CWL tags remaining.)
        if step_key not in subkeys:
            clt_args = wic_steps.get(f'({i+1}, {step_key})', {})
            if 'wic' in clt_args:
                # Do NOT add yml tags to the raw CWL!
                # We can simply leave any step-specific wic: tags at top-level.
                # Copy so we only delete from the step, not also the top-level.
                clt_args = copy.deepcopy(clt_args)
                del clt_args['wic']
            sub_yml_tree = clt_args
            args_provided_dict_self = {}
            if steps[i][step_key]:
                args_provided_dict_self = steps[i][step_key]
            # NOTE: To support overloading, the parent args must overwrite the child args!
            args_provided_dict = merge(args_provided_dict_self, sub_yml_tree,
                                       strategy=Strategy.TYPESAFE_REPLACE)  # TYPESAFE_ADDITIVE ?
            # Now mutably overwrite the self args with the merged args
            steps[i][step_key] = args_provided_dict

    return YamlTree(step_id, yaml_tree)


def tree_to_forest(yaml_tree_tuple: YamlTree, tools: Tools) -> YamlForest:
    """The purpose of this function is to abstract away the process of traversing an AST.

    Args:
        yaml_tree_tuple (YamlTree): A tuple of name and yml AST
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()

    Returns:
        YamlForest: A recursive data structure containing all sub-trees encountered while traversing the yml AST.
    """
    (step_id, yaml_tree) = yaml_tree_tuple

    wic = {'wic': yaml_tree.get('wic', {})}
    if 'backends' in wic['wic']:
        backends_forest_list = []
        for stepid, back in wic['wic']['backends'].items():
            backend_forest = (stepid, tree_to_forest(YamlTree(stepid, back), tools))
            backends_forest_list.append(backend_forest)
        return YamlForest(YamlTree(step_id, yaml_tree), backends_forest_list)

    steps: List[Yaml] = yaml_tree['steps']
    wic_steps = wic['wic'].get('steps', {})
    steps_keys = utils.get_steps_keys(steps)
    tools_stems = [stepid.stem for stepid in tools]
    subkeys = utils.get_subkeys(steps_keys, tools_stems)

    yaml_forest_list = []

    for i, step_key in enumerate(steps_keys):

        if step_key in subkeys:
            wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
            plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')

            sub_yaml_tree = steps[i][step_key]['subtree']
            sub_yml_forest = tree_to_forest(YamlTree(StepId(step_key, plugin_ns_i), sub_yaml_tree), tools)
            (sub_yml_tree_step_id, sub_yml_tree_) = sub_yml_forest.yaml_tree
            yaml_forest_list.append((sub_yml_tree_step_id, sub_yml_forest))

    return YamlForest(YamlTree(step_id, yaml_tree), yaml_forest_list)


def get_inlineable_subworkflows(yaml_tree_tuple: YamlTree,
                                tools: Tools,
                                backend: bool = False,
                                namespaces_init: Namespaces = []) -> List[Namespaces]:
    """Traverses a yml AST and finds all subworkflows which can be inlined into their parent workflow.

    Args:
        yaml_tree_tuple (YamlTree): A tuple of name and yml AST
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()
        backend (bool): True if the immediate parent workflow is a backend.
        namespaces_init (Namespaces): The initial subworkflow to start the traversal ([] == root)

    Returns:
        List[Namespaces]: The subworkflows which can be inlined into their parent workflows.
    """
    (step_id, yaml_tree) = yaml_tree_tuple
    yaml_name = step_id.stem

    # Check for top-level yml dsl args
    wic = {'wic': yaml_tree.get('wic', {})}

    if 'backends' in wic['wic']:
        # Use yaml_name (instead of back_name) and do not append to namespace_init.
        sub_namespaces_list = []
        for stepid, back in wic['wic']['backends'].items():
            sub_namespaces = get_inlineable_subworkflows(YamlTree(stepid, back), tools, True, namespaces_init)
            sub_namespaces_list.append(sub_namespaces)
        return utils.flatten(sub_namespaces_list)

    steps: List[Yaml] = yaml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)
    tools_stems = [stepid.stem for stepid in tools]
    subkeys = utils.get_subkeys(steps_keys, tools_stems)

    # All subworkflows are inlineable, except scattered subworkflows.
    inlineable = wic['wic'].get('inlineable', True)
    namespaces = [namespaces_init] if inlineable and namespaces_init != [] and not backend else []

    for i, step_key in enumerate(steps_keys):
        yaml_stem = Path(yaml_name).stem
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        if step_key in subkeys:
            sub_yml_tree = steps[i][step_key]['subtree']

            y_t = YamlTree(StepId(step_key, step_id.plugin_ns), sub_yml_tree)
            sub_namespaces = get_inlineable_subworkflows(y_t, tools, False, namespaces_init + [step_name_i])
            namespaces += sub_namespaces

    return namespaces


def inline_subworkflow(yaml_tree_tuple: YamlTree, namespaces: Namespaces) -> Tuple[YamlTree, int]:
    """Inlines the given subworkflow into its immediate parent workflow.

    Args:
        yaml_tree_tuple (YamlTree): A tuple of name and yml AST
        namespaces (Namespaces): Specifies the path in the yml AST to the subworkflow to be inlined.

    Returns:
        YamlTree: The updated root workflow with the given subworkflow inlined into its immediate parent workflow.
    """
    if namespaces == []:
        return yaml_tree_tuple, 0

    (step_id, yaml_tree) = copy.deepcopy(yaml_tree_tuple)
    yaml_name = step_id.stem

    wic = {'wic': yaml_tree.get('wic', {})}
    if 'backends' in wic['wic']:
        if len(namespaces) == 1:  # and namespaces[0] == yaml_name ?
            (back_name_, yaml_tree) = utils.extract_backend(yaml_tree, wic['wic'], Path(''))
            yaml_tree = {'steps': yaml_tree['steps']}  # Remove wic tag
            len_substeps = len(yaml_tree['steps'])
            return YamlTree(StepId(back_name_, step_id.plugin_ns), yaml_tree), 0  # len_substeps  # TODO: check step_id

        # Pass namespaces through unmodified
        backends_trees = []
        for stepid, back in wic['wic']['backends'].items():
            backend_tree, len_substeps = inline_subworkflow(YamlTree(stepid, back), namespaces)
            backends_trees.append(backend_tree)
        yaml_tree['wic']['backends'] = dict(backends_trees)
        return YamlTree(step_id, yaml_tree), 0  # choose len_substeps from which backend?

    steps: List[Yaml] = yaml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)
    yaml_stem = Path(yaml_name).stem
    step_names = [utils.step_name_str(yaml_stem, i, step_key)
                  for i, step_key in enumerate(steps_keys)]

    if namespaces[0] not in step_names:
        # This should never happen (if namespaces comes from get_inlineable_subworkflows)
        raise Exception(f'Error! {namespaces[0]} not in {step_names}')

    # TODO: We really need to inline the wic tags as well. This may be complicated
    # because due to overloading we may need to modify parent wic tags.

    (yaml_stem, i, step_key) = utils.parse_step_name_str(namespaces[0])
    sub_yml_tree = steps[i][step_key]['subtree']
    sub_parentargs = steps[i][step_key]['parentargs']

    len_substeps = 0
    if len(namespaces) == 1:
        steps_inits = steps[:i]  # Exclude step i
        steps_tails = steps[i+1:]  # Exclude step i
        # ~ syntax, specifically apply sub_parentargs to all inputs: call sites in sub_yml_tree
        sub_yml_tree = apply_args(sub_yml_tree, sub_parentargs)
        # Inline sub-steps.
        sub_steps: List[Yaml] = sub_yml_tree['steps']
        yaml_tree['steps'] = steps_inits + sub_steps + steps_tails
        # Need to re-index both the sub-step numbers as well as the
        # subsequent steps in this workflow? No, except for wic: steps:
        len_substeps = len(sub_steps)

        parent_wic_tag = wic.get('wic', {}).get("steps", {}).get(
            f'({i + 1}, {step_key})', {}).get('wic', {})
        sub_wic_tag = sub_yml_tree.get('wic', {})

        # TODO: need cleaner code to make arbitrary-depth dictionary.
        if 'wic' not in wic:
            wic['wic'] = {}
        if 'steps' not in wic['wic']:
            wic['wic']['steps'] = {}
        if f'({i + 1}, {step_key})' not in wic['wic']['steps']:
            wic['wic']['steps'][f'({i + 1}, {step_key})'] = {}

        # Merge parent into child to support overloading.
        # TODO: Need to sort the steps by index
        wic['wic']['steps'][f'({i + 1}, {step_key})']['wic'] = \
            merge(sub_wic_tag, parent_wic_tag, strategy=Strategy.TYPESAFE_REPLACE)
    else:
        # Strip off one initial namespace
        y_t = YamlTree(StepId(step_key, step_id.plugin_ns), sub_yml_tree)
        (step_key_, sub_yml_tree), len_substeps = inline_subworkflow(y_t, namespaces[1:])
        # TODO: re-index wic: steps: ? We probably should, although
        # inlineing after merging should not affect CWL args.
        # Re-indexing could be tricky w.r.t. overloading.
        # TODO: maintain inference boundaries (once feature is added)
        # NOTE: Since parentargs are applied after compiling a subworkflow,
        # and since inlineing removes the subworkflow, parentargs does not
        # appear to be inlineing invariant! However, using ~ syntax helps.
        steps[i][step_key] = {'subtree': sub_yml_tree, 'parentargs': sub_parentargs}

    yaml_tree['wic'] = inline_subworkflow_wic_tag(wic, namespaces, len_substeps)

    return YamlTree(step_id, yaml_tree), len_substeps


def apply_args(sub_yml_tree: Yaml, sub_parentargs: Yaml) -> Yaml:
    # Do we need to deepcopy? We are already deepcopy'ing at the only call site,
    # so looks like no.
    inputs_workflow = sub_yml_tree.get('inputs', {})
    if 'inputs' in sub_yml_tree:
        del sub_yml_tree['inputs']

    steps = sub_yml_tree['steps']
    steps_keys = utils.get_steps_keys(steps)

    for argkey in inputs_workflow:
        # Ordinarily edge inference works across subworkflow boundaries (i.e. is inlineing invariant),
        # but with ~ syntax in the subworkflow and no explicit arguments in the parent workflow,
        # we cannot blindly inline the subworkflow and remove the ~'s in the subworkflow.
        # TODO: Consider adding wic metadata tags to cause inference to skip past the beginning of the subworkflow.
        if argkey not in sub_parentargs.get('in', {}):
            print(f'Warning! Inlineing {argkey} with explicit inputs: in the subworkflow' +
                  'but edge inference in the parent workflow is not supported.')

    for argkey, argval in sub_parentargs.get('in', {}).items():
        # If we are attempting to apply a parameter given in the parent workflow,
        # that parameter had better exist in the subworkflow!
        if not argkey in inputs_workflow:
            raise Exception(f'Error while inlineing {argkey}\n{yaml.dump(sub_yml_tree)}\n{yaml.dump(sub_parentargs)}')

        for i, step_key in enumerate(steps_keys):
            # NOTE: We should probably be using
            # sub_keys = utils.get_subkeys(steps_keys, tools)
            # to check whether or not `step_key in sub_keys` and thus
            # whether or not to use ['parentargs']
            in_step = steps[i][step_key].get('in', {})  # CommandLineTools should have ['in'] (if any)
            if not in_step:
                # Subworkflows should have ['parentargs']['in'] (if any)
                in_step = steps[i][step_key].get('parentargs', {}).get('in', {})

            for inputkey, inputval in in_step.items():
                if inputval == '~' + argkey:
                    # overwrite ~ syntax / apply argval
                    in_step[inputkey] = argval

    return sub_yml_tree


def inline_subworkflow_wic_tag(wic_tag: Yaml, namespaces: Namespaces, len_substeps: int) -> Yaml:
    """Inlines the wic metadata tags associated with the given subworkflow into its immediate parent wic.

    Args:
        wic_tag (Yaml): The wicmetadata tag associated with the given workflow
        namespaces (Namespaces): Specifies the path in the yml AST to the subworkflow to be inlined.
        len_substeps (int): The number of steps in the subworkflow to be inlined.

    Returns:
        Yaml: The updated wic metadata tag with the wic metadata tag associated with the given subworkflow inlined.
    """
    tag_wic: Yaml = wic_tag['wic']

    # Note: the index after parsing is 0-based.
    step_ints_names = [utils.parse_step_name_str(ns)[1:] for ns in namespaces]

    sub_wic_parent = wic_tag  # initialize to the 'root' wic tag
    # Traverse down to the parent node of the subworkflow to the inlined
    for index, step_name in step_ints_names[:-1]:
        sub_wic_parent = sub_wic_parent.get('wic', {}).get('steps', {}).get(f'({index + 1}, {step_name})', {})
        # Note: if any of the intermediate workflows in the path in the AST tree
        # from the current workflow to the subworkflow being inlined is absent in the current
        # wic metadata tag, the inlining won't have any effect on the wic tag of this workflow.
        # Note: When there're other options like 'graphviz' but not 'steps', we can also short
        # circuit and return.
        if 'steps' not in sub_wic_parent.get('wic', {}):
            return tag_wic  # If path does not exist, do nothing and short circuit

    # Then get the wic tag of the subworkflow
    # Note: sub_index is 0-based.
    sub_index, sub_step_name = step_ints_names[-1]
    sub_wic = sub_wic_parent.get('wic', {}).get('steps', {}).get(f'({sub_index + 1}, {sub_step_name})', {})

    # Note: we should not short circuit when the subworkflow being inlined is not used in the
    # current wic tag, since inlining it will affect the indices of sibling steps following it.
    sub_wic_steps_reindexed = utils.reindex_wic_steps(sub_wic.get('wic', {}).get('steps', {}), 1, sub_index)

    # Delete the subworkflow from the parent workflow since it is replaced by its internal steps.
    # This needs to be explicitly done since the key of this subworkflow in the dict is not
    # the same as any of its inlined steps and therefore won't be overwritten by the deep merge.
    if f'({sub_index + 1}, {sub_step_name})' in sub_wic_parent.get('wic', {}).get('steps', {}):
        del sub_wic_parent['wic']['steps'][f'({sub_index + 1}, {sub_step_name})']

    # The inlining is actually a replacement of the target subworkflows by its steps.
    # Therefore, the incrementing count should be len_substeps - 1.
    sub_wic_parent_steps_reindexed = utils.reindex_wic_steps(sub_wic_parent['wic']['steps'],
                                                             sub_index + 1, len_substeps - 1)

    # Merge the wic: steps: dicts and mutably update the parent
    # Merge parent into child to support overloading.
    # TODO: The 'ranksame' in the wic tag of the inlined subworkflow is ignored
    # and not merged for now.
    sub_wic_parent['wic']['steps'] = merge(sub_wic_steps_reindexed, sub_wic_parent_steps_reindexed,
                                           strategy=Strategy.TYPESAFE_REPLACE)

    return tag_wic


def move_slash_last(source_new: str) -> str:
    """Move / to the last ___ position\n
       (Moving to the last position works because we are inlineing recursively.)

    Args:
        source_new (str): A string representing a CWL dependency, i.e. containing /

    Returns:
        str: source_new with / moved to the last ___ position
    """
    if '/' in source_new:
        source_new = source_new.replace('/', '___')
        source_split = source_new.split('___')
        source_new = '___'.join(source_split[:-1]) + '/' + source_split[-1]
        return source_new

    return source_new


def inline_subworkflow_cwl(rose_tree: RoseTree) -> RoseTree:
    """Inlines all compiled CWL subworkflows into the root workflow.

    Args:
        rose_tree (RoseTree): The data associated with compiled subworkflows

    Returns:
        RoseTree: The updated root workflow with all compiled CWL subworkflows recursively inlined.
    """
    # NOTE: This code is a little bit nasty, and I absolutely do not guarantee that it won't break in the future.
    if rose_tree.sub_trees == []:
        return rose_tree

    sub_trees = [inline_subworkflow_cwl(t) for t in rose_tree.sub_trees]

    node_data: NodeData = rose_tree.data
    cwl_tree = copy.deepcopy(node_data.compiled_cwl)
    # print('cwl_tree', yaml.dump(cwl_tree))
    # print('subtrees')
    # for t in rose_tree.sub_trees:
    #    print(yaml.dump(t.data.compiled_cwl))

    steps = cwl_tree['steps']
    steps_keys = list(steps.keys())
    # NOTE: Only use the last namespace since we are recursively inlineing.
    subkeysdict = {t.data.namespaces[-1]: copy.deepcopy(t.data.compiled_cwl)
                   for t in sub_trees}  # NOT rose_tree.sub_trees
    # print('subkeys', list(subkeysdict.keys()))
    # print('steps_keys', steps_keys)
    steps_new = {}

    count = 0
    for i, step_key in enumerate(steps_keys):
        if step_key in list(subkeysdict.keys()):
            count += 1  # Check that we inline all subworkflows
            inputs = steps[step_key]['in']
            scattervars = steps[step_key].get('scatter', [])

            sub_cwl_tree = subkeysdict[step_key]
            sub_steps = sub_cwl_tree['steps']
            sub_steps_new = {}
            for substepkey, substepval in sub_steps.items():
                substep_inputs = substepval['in']
                substep_inputs_new = {}
                for subinputkey, subinputval in substep_inputs.items():
                    # By default, copy the inputs and prepend namespace
                    if isinstance(subinputval, str):
                        source = move_slash_last(subinputval)
                        substep_inputs_new[subinputkey] = step_key + '___' + subinputval

                    if isinstance(subinputval, Dict):
                        source = subinputval['source']
                        source_new = move_slash_last(subinputval['source'])
                        subinputval['source'] = step_key + '___' + source_new
                        substep_inputs_new[subinputkey] = subinputval

                    if source in inputs:
                        # Replace the formal parameter in the subworkflow with
                        # the actual parameter in the parent workflow.
                        newval = inputs[source]

                        if isinstance(newval, str):
                            source_new = move_slash_last(newval)
                            # NOTE: Do not namespace; already namespaced in parent workflow.
                            newval = source_new  # step_key + '___' + source_new

                        if isinstance(newval, Dict) and 'source' in newval:
                            source_new = move_slash_last(newval['source'])
                            # NOTE: Do not namespace; already namespaced in parent workflow.
                            newval['source'] = source_new  # step_key + '___' + source_new

                        substep_inputs_new[subinputkey] = newval  # Overwrite
                        # Copy any input variables referenced, i.e.
                        # initial scatter and/or slice for step 1
                        m = re.match(r'.*\[inputs\.(.*)\].*', str(newval))
                        if m:
                            inputvarname = m.groups()[0]
                            if inputvarname:
                                substep_inputs_new[inputvarname] = inputs[inputvarname]
                                if inputvarname in scattervars:
                                    if 'scatter' in substepval:
                                        substepval['scatter'] += [inputvarname]
                                    else:
                                        substepval['scatter'] = [inputvarname]

                    # Distribute scatter unconditionally across ALL subworkflow dependencies
                    # i.e. https://en.wikipedia.org/wiki/Distributive_property
# NOTE: This code assumes the user has manually performed https://en.wikipedia.org/wiki/Loop-invariant_code_motion
# on the yml. In other words, it assumes that the user has separated / extracted all non-scattered steps from all
# steps that should be scattered. i.e. 1 receptor vs N ligands. Otherwise, we need to transitively follow the edges
# until we can determine the cardinality. It may be possible to avoid the transitive search by bootstrapping in-order,
# but for now let's require the user to manually modify their yml.
                    if scattervars:
                        if ((isinstance(subinputval, str) and '/' in subinputval) or
                                (isinstance(subinputval, Dict) and '/' in subinputval['source'])):
                            if 'scatter' in substepval:
                                if subinputkey not in substepval['scatter']:
                                    substepval['scatter'] += [subinputkey]
                            else:
                                substepval['scatter'] = [subinputkey]
                            substepval['scatterMethod'] = 'dotproduct'

                # Overwrite inputs
                substepval['in'] = substep_inputs_new

                # Modify run tag
                runstr = substepval['run']
                if runstr.startswith('../'):
                    substepval['run'] = runstr[len('../'):]
                # TODO: Consider general case of prepending namespace / directory

                # prepend namespace to step names
                namespaced = step_key + '___' + substepkey
                sub_steps_new[namespaced] = substepval

            sub_cwl_tree['steps'] = sub_steps_new

            # Insert the steps from the subworkflow
            steps_new.update(sub_steps_new)
        else:
            # Otherwise, just copy the step
            steps_new[step_key] = steps[step_key]

    if count != len(subkeysdict):
        print('Error! Not all subworkflows inlined!')

    cwl_tree['steps'] = steps_new

    # Finally, for all outputs in the parent workflow
    outputs = cwl_tree['outputs']
    outputs_new = {}
    for outkey, outval in outputs.items():
        if 'output_all' in outkey:
            continue  # Skip for now.

        outval['outputSource'] = move_slash_last(outval['outputSource'])
        outputs_new[outkey] = outval

    cwl_tree['outputs'] = outputs_new

    data = NodeData(node_data.namespaces, node_data.name, node_data.yml, cwl_tree,  # NOTE: Only updating cwl_tree
                    node_data.workflow_inputs_file, node_data.explicit_edge_defs,
                    node_data.explicit_edge_calls, node_data.graph,
                    node_data.inputs_workflow, node_data.step_name_1)

    # print('cwl_tree', yaml.dump(cwl_tree))
    # print('subtrees')
    # for t in rose_tree.sub_trees:
    #    print(yaml.dump(t.data.compiled_cwl))

    return RoseTree(data, [])
