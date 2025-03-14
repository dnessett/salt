import copy
import glob
import logging
import os
import re

from jinja2 import Environment, FileSystemLoader

import salt.utils.odict
import salt.utils.path
import salt.utils.yaml

log = logging.getLogger(__name__)


# Renders jinja from a template file
def render_jinja(_file, salt_data):
    j_env = Environment(loader=FileSystemLoader(os.path.dirname(_file)))
    j_env.globals.update(
        {
            "__opts__": salt_data["__opts__"],
            "__salt__": salt_data["__salt__"],
            "__grains__": salt_data["__grains__"],
            "__pillar__": salt_data["__pillar__"],
            "minion_id": salt_data["minion_id"],
        }
    )
    j_render = j_env.get_template(os.path.basename(_file)).render()
    return j_render


# Renders yaml from rendered jinja
def render_yaml(_file, salt_data):
    return salt.utils.yaml.safe_load(render_jinja(_file, salt_data))


# Returns a dict from a class yaml definition
def get_class(_class, salt_data):
    l_files = []
    saltclass_path = salt_data["path"]

    straight, sub_init, sub_straight = get_class_paths(_class, saltclass_path)

    for root, dirs, files in salt.utils.path.os_walk(
        os.path.join(saltclass_path, "classes"), followlinks=True
    ):
        for l_file in files:
            l_files.append(os.path.join(root, l_file))

    if straight in l_files:
        return render_yaml(straight, salt_data)

    if sub_straight in l_files:
        return render_yaml(sub_straight, salt_data)

    if sub_init in l_files:
        return render_yaml(sub_init, salt_data)

    log.warning("%s: Class definition not found", _class)
    return {}


def get_class_paths(_class, saltclass_path):
    """
    Converts the dotted notation of a saltclass class to its possible file counterparts.

    :param str _class: Dotted notation of the class
    :param str saltclass_path: Root to saltclass storage
    :return: 3-tuple of possible file counterparts
    :rtype: tuple(str)
    """
    straight = os.path.join(saltclass_path, "classes", f"{_class}.yml")
    sub_straight = os.path.join(
        saltclass_path, "classes", "{}.yml".format(_class.replace(".", os.sep))
    )
    sub_init = os.path.join(
        saltclass_path, "classes", _class.replace(".", os.sep), "init.yml"
    )
    return straight, sub_init, sub_straight


def get_class_from_file(_file, saltclass_path):
    """
    Converts the absolute path to a saltclass file back to the dotted notation.

    .. code-block:: python

       print(get_class_from_file('/srv/saltclass/classes/services/nginx/init.yml', '/srv/saltclass'))
       # services.nginx

    :param str _file: Absolute path to file
    :param str saltclass_path: Root to saltclass storage
    :return: class name in dotted notation
    :rtype: str
    """
    # remove classes path prefix
    _file = _file[len(os.path.join(saltclass_path, "classes")) + len(os.sep) :]
    # remove .yml extension
    _file = _file[:-4]
    # revert to dotted notation
    _file = _file.replace(os.sep, ".")
    # remove tailing init
    if _file.endswith(".init"):
        _file = _file[:-5]
    return _file


# Return environment
def get_env_from_dict(exp_dict_list):
    environment = ""
    for s_class in exp_dict_list:
        if "environment" in s_class:
            environment = s_class["environment"]
    return environment


# Merge dict b into a
def dict_merge(a, b, path=None):
    if path is None:
        path = []

    for key in b:
        if key in a:
            if isinstance(a[key], list) and isinstance(b[key], list):
                if b[key][0] == "^":
                    b[key].pop(0)
                    a[key] = b[key]
                else:
                    a[key].extend(b[key])
            elif isinstance(a[key], dict) and isinstance(b[key], dict):
                dict_merge(a[key], b[key], path + [str(key)])
            elif a[key] == b[key]:
                pass
            else:
                a[key] = b[key]
        else:
            a[key] = b[key]
    return a


# Recursive search and replace in a dict
def dict_search_and_replace(d, old, new, expanded):
    for k, v in d.items():
        if isinstance(v, dict):
            dict_search_and_replace(d[k], old, new, expanded)

        if isinstance(v, list):
            x = 0
            for i in v:
                if isinstance(i, dict):
                    dict_search_and_replace(v[x], old, new, expanded)
                if isinstance(i, str):
                    if i == old:
                        v[x] = new
                x = x + 1

        if v == old:
            d[k] = new

    return d


# Retrieve original value from ${xx:yy:zz} to be expanded
def find_value_to_expand(x, v):
    a = x
    for i in v[2:-1].split(":"):
        if a is None:
            return v
        if i in a:
            a = a.get(i)
        else:
            return v
    return a


# Look for regexes and expand them
def find_and_process_re(_str, v, k, b, expanded):
    vre = re.finditer(r"(^|.)\$\{.*?\}", _str)
    if vre:
        for re_v in vre:
            re_str = str(re_v.group())
            if re_str.startswith("\\"):
                v_new = _str.replace(re_str, re_str.lstrip("\\"))
                b = dict_search_and_replace(b, _str, v_new, expanded)
                expanded.append(k)
            elif not re_str.startswith("$"):
                v_expanded = find_value_to_expand(b, re_str[1:])
                v_new = _str.replace(re_str[1:], v_expanded)
                b = dict_search_and_replace(b, _str, v_new, expanded)
                _str = v_new
                expanded.append(k)
            else:
                v_expanded = find_value_to_expand(b, re_str)
                if isinstance(v, str):
                    v_new = v.replace(re_str, v_expanded)
                else:
                    v_new = _str.replace(re_str, v_expanded)
                b = dict_search_and_replace(b, _str, v_new, expanded)
                _str = v_new
                v = v_new
                expanded.append(k)
    return b


# Return a dict that contains expanded variables if found
def expand_variables(a, b, expanded, path=None):
    if path is None:
        b = a.copy()
        path = []

    for k, v in a.items():
        if isinstance(v, dict):
            expand_variables(v, b, expanded, path + [str(k)])
        else:
            if isinstance(v, list):
                for i in v:
                    if isinstance(i, dict):
                        expand_variables(i, b, expanded, path + [str(k)])
                    if isinstance(i, str):
                        b = find_and_process_re(i, v, k, b, expanded)

            if isinstance(v, str):
                b = find_and_process_re(v, v, k, b, expanded)
    return b


def match_class_glob(_class, saltclass_path):
    """
    Takes a class name possibly including `*` or `?` wildcards (or any other wildcards supportet by `glob.glob`) and
    returns a list of expanded class names without wildcards.

    .. code-block:: python

       classes = match_class_glob('services.*', '/srv/saltclass')
       print(classes)
       # services.mariadb
       # services.nginx...


    :param str _class: dotted class name, globbing allowed.
    :param str saltclass_path: path to the saltclass root directory.

    :return: The list of expanded class matches.
    :rtype: list(str)
    """
    straight, sub_init, sub_straight = get_class_paths(_class, saltclass_path)
    classes = []
    matches = []
    matches.extend(glob.glob(straight))
    matches.extend(glob.glob(sub_straight))
    matches.extend(glob.glob(sub_init))
    if not matches:
        log.warning("%s: Class globbing did not yield any results", _class)
    for match in matches:
        classes.append(get_class_from_file(match, saltclass_path))
    return classes


def expand_classes_glob(classes, salt_data):
    """
    Expand the list of `classes` to no longer include any globbing.

    :param iterable(str) classes: Iterable of classes
    :param dict salt_data: configuration data
    :return: Expanded list of classes with resolved globbing
    :rtype: list(str)
    """
    all_classes = []
    expanded_classes = []
    saltclass_path = salt_data["path"]

    for _class in classes:
        all_classes.extend(match_class_glob(_class, saltclass_path))

    for _class in all_classes:
        if _class not in expanded_classes:
            expanded_classes.append(_class)

    return expanded_classes


def expand_classes_in_order(minion_dict, salt_data, seen_classes, classes_to_expand):
    """
    Expand the list of `classes_to_expand` and return them in the order found

    The return order is `[C, B, A, M, B, L, MINION_ID]` when:

    - minion node include classes `A` and `L`
    - `A` include class `B`
    - `B` include class `C`
    - `L` include class `M` and `B`

    :param dict minion_dict: definition of minion node
    :param dict salt_data: configuration data
    :param iterable(str) seen_classes: classes already processed
    :param iterable(str) classes_to_expand: classes to recursivly expand
    :return: Expanded classes in proper order
    :rtype: salt.utils.odict.OrderedDict
    """

    expanded_classes = salt.utils.odict.OrderedDict()

    # Get classes to expand from minion dictionary
    if not classes_to_expand and "classes" in minion_dict:
        classes_to_expand = minion_dict["classes"]

    classes_to_expand = expand_classes_glob(classes_to_expand, salt_data)

    # Now loop on list to recursively expand them
    for klass in classes_to_expand:
        if klass not in seen_classes:
            seen_classes.append(klass)
            klass_dict = salt.utils.odict.OrderedDict(
                {klass: get_class(klass, salt_data)}
            )
            # Fix corner case where class is loaded but doesn't contain anything
            if klass_dict[klass] is None:
                klass_dict[klass] = {}

            # Merge newly found pillars into existing ones
            new_pillars = klass_dict[klass].get("pillars", {})
            if new_pillars:
                dict_merge(salt_data["__pillar__"], new_pillars)

            if "classes" in klass_dict[klass]:
                nested_classes = expand_classes_in_order(
                    {},
                    salt_data,
                    seen_classes,
                    klass_dict[klass].get("classes", {}),
                )

                # Put current class after nested classes
                klass_dict.update(nested_classes)
                klass_dict.move_to_end(klass)

            expanded_classes.update(klass_dict)

    # Minion dict must be at the end
    if minion_dict:
        expanded_classes.update({salt_data["minion_id"]: minion_dict})

    return expanded_classes


def expanded_dict_from_minion(minion_id, salt_data):
    _file = ""
    saltclass_path = salt_data["path"]
    # Start
    for root, dirs, files in salt.utils.path.os_walk(
        os.path.join(saltclass_path, "nodes"), followlinks=True
    ):
        for minion_file in files:
            if minion_file == f"{minion_id}.yml":
                _file = os.path.join(root, minion_file)

    # Load the minion_id definition if existing, else an empty dict
    node_dict = {}
    if _file:
        node_dict[minion_id] = render_yaml(_file, salt_data)
    else:
        log.warning("%s: Node definition not found", minion_id)
        node_dict[minion_id] = {}

    # Merge newly found pillars into existing ones
    dict_merge(
        salt_data["__pillar__"], copy.deepcopy(node_dict[minion_id]).get("pillars", {})
    )

    # Get 2 ordered lists:
    # expanded_classes: A list of all the dicts
    # classes_list: List of all the classes
    expanded_classes = expand_classes_in_order(node_dict[minion_id], salt_data, [], [])

    # Here merge the pillars together
    pillars_dict = {}
    states_list = []
    classes_list = list(expanded_classes.keys())[:-1]
    classes_values = list(expanded_classes.values())
    for exp_dict in classes_values:
        if "pillars" in exp_dict:
            dict_merge(pillars_dict, exp_dict)
        if "states" in exp_dict:
            states_list.extend(exp_dict["states"])

    # Avoid duplicates, keep first
    state_seen = set()
    states_list = [
        state
        for state in states_list
        if not (state in state_seen or state_seen.add(state))
    ]

    return classes_values, pillars_dict, classes_list, states_list


def get_pillars(minion_id, salt_data):
    # Get 2 dicts and 2 lists
    # expanded_classes: Full list of expanded dicts
    # pillars_dict: dict containing merged pillars in order
    # classes_list: All classes processed in order
    # states_list: All states listed in order
    (
        expanded_classes,
        pillars_dict,
        classes_list,
        states_list,
    ) = expanded_dict_from_minion(minion_id, salt_data)

    # Retrieve environment
    environment = get_env_from_dict(expanded_classes)

    # Expand ${} variables in merged dict
    # pillars key shouldn't exist if we haven't found any minion_id ref
    if "pillars" in pillars_dict:
        pillars_dict_expanded = expand_variables(pillars_dict["pillars"], {}, [])
    else:
        pillars_dict_expanded = expand_variables({}, {}, [])

    # Build the final pillars dict
    pillars_dict = {}
    pillars_dict["__saltclass__"] = {}
    pillars_dict["__saltclass__"]["states"] = states_list
    pillars_dict["__saltclass__"]["classes"] = classes_list
    pillars_dict["__saltclass__"]["environment"] = environment
    pillars_dict["__saltclass__"]["nodename"] = minion_id
    pillars_dict.update(pillars_dict_expanded)

    return pillars_dict


def get_tops(minion_id, salt_data):
    # Get 2 dicts and 2 lists
    # expanded_classes: Full list of expanded dicts
    # pillars_dict: dict containing merged pillars in order
    # classes_list: All classes processed in order
    # states_list: All states listed in order
    (
        expanded_classes,
        pillars_dict,
        classes_list,
        states_list,
    ) = expanded_dict_from_minion(minion_id, salt_data)

    # Retrieve environment
    environment = get_env_from_dict(expanded_classes)

    # Build final top dict
    tops_dict = {}
    tops_dict[environment] = states_list

    return tops_dict
