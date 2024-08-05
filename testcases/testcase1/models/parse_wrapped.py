import os
import re
import xml.etree.ElementTree as ET 
import warnings
import argparse
import sys
import json

READ = 0
OVERWRITE = 1
KPI = 2

def parse_XML(path):
    '''Parse the signal exchange block class instances using the dae xml (obtained via dumpDAEXML(model))

    Parameters
    ----------
    path : str
        Path to xml to be parsed. 

    Returns
    -------
    instances : dict
        Dictionary of overwrite and read block class instance lists.
        {'Overwrite': {input_name : {Unit : unit_name, Description : description, Minimum : min, Maximum : max}},
         'Read': {output_name : {Unit : unit_name, Description : description, Minimum : min, Maximum : max}}}
    signals : dict
        {'signal_type' : [output_name]}
    '''
    tree = ET.parse(path)
    instances = {'Overwrite':dict(), 'Read':dict()}
    signals = {}
    vars = list(tree.iter("variable"))
    for var in vars:
        flag = None
        name = var.get("name")
        if 'boptestRead' in name:
            flag = READ
        elif 'boptestOverwrite' in name:
            flag = OVERWRITE
        elif 'KPIs' in name:
            flag = KPI
        if flag is None:
            continue
        elif flag == KPI:
            _process_KPI(tree, var, signals, name)
        else:
            _process_readwrite(tree, name, instances, flag)
    return instances, signals

def create_wrapper(model_path, instances):
    '''Write the wrapper modelica model and export as fmu

    Parameters
    ----------
    model_path : str
        Path to orginal modelica model. Needed to extend such model. This needs to be provided in the Modelica format e.g. "Package.SubPackage.Model"
    instances : dict
        Dictionary of overwrite and read block class instance lists.
        {'Overwrite': [str], 'Read': [str]}

    Returns
    -------
    fmu_path : str
        Path to the wrapped modelica model fmu
    wrapped_path : str or None
        Path to the wrapped modelica model if instances of signale exchange.
        Otherwise, None

    '''

    # Check for instances of Overwrite and/or Read blocks
    len_write_blocks = len(instances['Overwrite'])
    len_read_blocks = len(instances['Read'])
    if not (len_write_blocks + len_read_blocks):
        warnings.warn('No signal exchange block instances found in model.  Exporting model as is.')
        return None

    model_name = 'wrapped'
    model_code = ['model {0} "Wrapped model of {1}"\n\t// Input overwrite\n'.format(model_name, model_path)]

    input_signals_w_info = dict()
    input_signals_wo_info = dict()
    input_activate_w_info = dict()
    input_activate_wo_info = dict()
    for block in instances['Overwrite'].keys():
        # Add to signal input list with and without units
        input_signals_w_info[block] = _make_var_name(block,style='input_signal',description=instances['Overwrite'][block]['Description'],attribute='(unit="{0}", min={1}, max={2})'.format(instances['Overwrite'][block]['Unit'], instances['Overwrite'][block]['Minimum'], instances['Overwrite'][block]['Maximum']))
        input_signals_wo_info[block] = _make_var_name(block,style='input_signal')
        # Add to signal activate list
        input_activate_w_info[block] = _make_var_name(block,style='input_activate',description='Activation for {0}'.format(instances['Overwrite'][block]['Description']))
        input_activate_wo_info[block] = _make_var_name(block,style='input_activate')
        # Instantiate input signal
        model_code.append('\tModelica.Blocks.Interfaces.RealInput {0};\n'.format(input_signals_w_info[block], block))
        # Instantiate input activation
        model_code.append('\tModelica.Blocks.Interfaces.BooleanInput {0};\n'.format(input_activate_w_info[block], block))
        # Add outputs for every read block and overwrite block
    model_code.append('\t// Out read\n')
    for i in ['Read', 'Overwrite']:
        for block in instances[i].keys():
            # Instantiate signal
            model_code.append('\tModelica.Blocks.Interfaces.RealOutput {0} = mod.{1}.y "{2}";\n'.format(_make_var_name(block,style='output',attribute='(unit="{0}")'.format(instances[i][block]['Unit'])), block, instances[i][block]['Description']))
            # Add original model
    model_code.append('\t// Original model\n')
    model_code.append('\t{0} mod(\n'.format(model_path))
    # Connect inputs to original model overwrite and activate signals
    if len_write_blocks:
        for i,block in enumerate(instances['Overwrite']):
            model_code.append('\t\t{0}(uExt(y={1}),activate(y={2}))'.format(block, input_signals_wo_info[block], input_activate_wo_info[block]))
            if i == len(instances['Overwrite'])-1:
                model_code.append(') "Original model with overwrites";\n')
            else:
                model_code.append(',\n')
    else:
        model_code.append(') "Original model without overwrites";\n')
        # End file -- with hard line ending
    model_code.append('end {};\n'.format(model_name))

    return ''.join(model_code)

def _process_KPI(tree, var, signals, instance):
    signal_type = var.find("bindExpression").attrib["string"].split(".")[-1]
    basename = ".".join(instance.split('.')[:-1])
    if signal_type in ['AirZoneTemperature',
                       'RadiativeZoneTemperature',
                       'OperativeZoneTemperature',
                       'RelativeHumidity',
                       'CO2Concentration']:
        signal_type = '{0}[{1}]'.format(signal_type, _get_final_value(tree, basename+'.zone'))
    if signal_type in signals:
        signals[signal_type].append(_make_var_name(basename,style='output'))
    else:
        signals[signal_type] = [_make_var_name(basename,style='output')]

def _process_readwrite(tree, instance, instances, flag):
    label = "Read" if flag == READ else "Overwrite"
    # get unit
    basename = '.'.join(instance.split('.')[:-1])
    signal_suffix = '.y' if flag == READ else '.u'
    node = next(filter(lambda x: x.attrib.get("name", "") == basename + signal_suffix, tree.getroot().iter()))
    unit = node.find('attributesValues').find('unit').attrib["string"].split('"')[1]
    # get description
    dnode = next(filter(lambda x: x.attrib.get("name", "") == basename + '.description', tree.getroot().iter()))
    description = dnode.find('bindExpression').attrib['string'].split('"')[1]
    # get minimum, maximum
    if flag == READ:
        mini = None
        maxi = None
    else:
        mini = float(node.find('attributesValues').find('minValue').attrib['string'])
        maxi = float(node.find('attributesValues').find('maxValue').attrib['string'])
    instances[label][basename] = {'Unit' : unit, 'Description' : description, 'Minimum' : mini, 'Maximum' : maxi}
    

def _get_final_value(tree, var):
    """
    Follow a string of aliasses to get to the literal expression
    """
    node = next(filter(lambda x: x.attrib.get("name", "") == var, tree.getroot().iter()))
    s = node.find("bindExpression").attrib["string"]
    literal = '"'
    if literal in s:
        return s.split(literal)[1]
    else:
        return _get_final_value(tree, s)



def _make_var_name(block, style, description='', attribute=''):
    # General modification
    name = block.replace('.', '_')
    # Handle empty descriptions
    if description == '':
        description = ''
    else:
        description = ' "{0}"'.format(description)

    # Specific modification
    if style == 'input_signal':
        var_name = '{0}_u{1}{2}'.format(name,attribute, description)
    elif style == 'input_activate':
        var_name = '{0}_activate{1}'.format(name, description)
    elif style == 'output':
        var_name = '{0}_y{1}{2}'.format(name,attribute, description)
    else:
        raise ValueError('Style {0} unknown.'.format(style))

    return var_name

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Parse an XML DAE description file and create a wrapper Modelica model')
    parser.add_argument("inputfile", metavar="FILE", help="Path to XML DAE description file. These files can be obtained by the omc command 'dumpXMLDAE'")
    parser.add_argument("-o", "--output", help="Path to write the wrapped model to. If no path is provided, write to stdout", action='store') 
    parser.add_argument("-m", "--model", help="Name of the model", action='store') 
    parser.add_argument("-j", "--json", help="Path to write the kpis.json file to. If no path is provided don't generate kpis.json", action='store')

    args = parser.parse_args()

    instances, signal_types = parse_XML(args.inputfile)
    wrapper_code = create_wrapper(args.model, instances)
    if args.output is None:
        print(wrapper_code)
    else:
        with open(args.output, 'w') as fh:
            fh.write(wrapper_code)
    if not args.json is None:
        with open(args.json, 'w') as fh:
            json.dump(signal_types, fh)