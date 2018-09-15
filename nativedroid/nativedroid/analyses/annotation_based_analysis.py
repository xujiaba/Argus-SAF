import logging
from nativedroid.analyses.resolver.armel_resolver import ArmelResolver
from nativedroid.analyses.resolver.x86_resolver import X86Resolver
from nativedroid.analyses.resolver.jni.jni_helper import *
from nativedroid.analyses.resolver.annotation.data_type_annotation import *
from nativedroid.analyses.resolver.model.__android_log_print import *
from cStringIO import StringIO

nativedroid_logger = logging.getLogger('AnnotationBasedAnalysis')
nativedroid_logger.setLevel(logging.INFO)

annotation_location = {
    'from_reflection_call': '~',
    'from_native': '~',
    'from_class': '~'
}


# logging.getLogger('angr.analyses.cfg.cfg_accurate').setLevel(logging.DEBUG)


class AnnotationBasedAnalysis(angr.Analysis):
    """
    This class performs taint analysis based upon angr's annotation technique.
    """

    def __init__(self, ssm, jni_method_addr, jni_method_arguments, is_native_pure, native_pure_info=None):
        """
        init

        :param SourceAndSinkManager ssm:
        :param str jni_method_addr: address of jni method
        :param str jni_method_arguments:
        :param list is_native_pure: whether it is pure native and android_main type or direct type.
        :param Object native_pure_info: initial SimState and native pure argument
        """
        if self.project.arch.name is 'ARMEL':
            self._resolver = ArmelResolver(self.project)
        elif self.project.arch.name is 'X86':
            self._resolver = X86Resolver(self.project)
        else:
            raise ValueError('Unsupported architecture: %d' % self.project.arch.name)
        self._hook_system_calls()
        self._ssm = ssm
        self._jni_method_addr = jni_method_addr
        if is_native_pure:
            self._state = self._resolver.prepare_native_pure_state(native_pure_info)
        else:
            self._state, self._arguments_summary = self._resolver.prepare_initial_state(jni_method_arguments)

        if is_native_pure:
            # nativedroid_logger.info('Native Activity, keep_state=True')
            self.cfg = self.project.analyses.CFGAccurate(fail_fast=True, starts=[self._jni_method_addr],
                                                         initial_state=self._state, context_sensitivity_level=1,
                                                         keep_state=True, normalize=True)
        else:
            self.cfg = self.project.analyses.CFGAccurate(fail_fast=True, starts=[self._jni_method_addr],
                                                         initial_state=self._state, context_sensitivity_level=1,
                                                         keep_state=True, normalize=True)
        # plot_cfg(self.cfg, 'CFG', asminst=True, remove_imports=False, remove_path_terminator=False)

    def _hook_system_calls(self):
        if '__android_log_print' in self.project.loader.main_object.imports:
            self.project.hook_symbol('__android_log_print', AndroidLogPrint(), replace=True)

    def count_cfg_instructions(self):
        """
        Count instructions size from CFG.

        :return: Instructions size
        :rtype: int
        """
        total_instructions = 0
        for func_addr, func in self.cfg.kb.functions.iteritems():
            func_instructions = 0
            # print func.name
            for block in func.blocks:
                block_instructions = len(block.instruction_addrs)
                # print block, block_instructions
                func_instructions += block_instructions
            total_instructions += func_instructions
        # print('Total INS: %d' % total_instructions)
        return total_instructions

    def _collect_taint_sources(self):
        """
        Collect source nodes from CFG.

        :return: A dictionary contains source nodes with its source tags (positions, taint_tags).
        :rtype: list
        """
        sources_annotation = set()
        for node in self.cfg.nodes():
            if node.is_simprocedure and node.name is 'SetObjectField':
                node_arg_value = node.input_state.regs.r1
                field_taint = False
                for annotation in node_arg_value.annotations:
                    if type(annotation) is JobjectAnnotation:
                        for field_info in annotation.fields_info:
                            if field_info.taint_info['is_taint'] and \
                                    field_info.taint_info['taint_type'][0] == '_SOURCE_' and \
                                    field_info.taint_info['taint_type'][1] != '_ARGUMENT_':
                                sources_annotation.add(annotation)
                                field_taint = True
                            else:
                                if type(field_info) is JobjectAnnotation:
                                    for info in field_info.fields_info:
                                        if info.taint_info['is_taint'] and \
                                                info.taint_info['taint_type'][0] == '_SOURCE_' and \
                                                info.taint_info['taint_type'][1] != '_ARGUMENT_':
                                            sources_annotation.add(annotation)
                                            field_taint = True
                        if field_taint is False and annotation.taint_info['is_taint'] and \
                                annotation.taint_info['taint_type'][0] == '_SOURCE_' and \
                                annotation.taint_info['taint_type'][1] != '_ARGUMENT_':
                            sources_annotation.add(annotation)

            elif node.is_simprocedure and node.name.startswith('Call'):
                for final_state in node.final_states:
                    node_return_value = final_state.regs.r0
                    for annotation in node_return_value.annotations:
                        if type(annotation) is JobjectAnnotation or type(annotation) is JstringAnnotation:
                            if annotation.taint_info['is_taint'] is True and \
                                    annotation.taint_info['taint_type'] == ['_SOURCE_', '_API_']:
                                sources_annotation.add(annotation)
        return sources_annotation

    def _collect_taint_sinks(self):
        """
        Collect sink nodes from CFG.

        :return: A dictionary contains sink nodes with its sink tags (positions, taint_tags).
        :rtype: dict
        """
        sink_nodes = {}
        sinks = list()
        sink_annotations = set()
        for node in self.cfg.nodes():
            if node.is_simprocedure and node.name.startswith('Call'):
                for final_state in node.final_states:
                    node_return_value = final_state.regs.r0
                    for annotation in node_return_value.annotations:
                        if type(annotation) is JobjectAnnotation or type(annotation) is JstringAnnotation:
                            if annotation.taint_info['is_taint'] and annotation.taint_info['taint_type'] == '_SINK_':
                                sink_annotations.add(annotation)
            fn = self.cfg.project.kb.functions.get(node.addr)
            if fn is not None:
                if self._ssm.is_sink(fn):
                    sink_tag = self._ssm.get_sink_tags(fn)
                    sink_nodes[node] = sink_tag
        for sink, (positions, tags) in sink_nodes.iteritems():
            input_state = sink.input_state
            final_states = sink.final_states
            args = self._resolver.get_taint_args(input_state, final_states, positions, tags)
            if args:
                nativedroid_logger.debug('tainted: %s, belong_obj: %s' % (args, sink.final_states[0].regs.r0))
                sinks.append(args)
        for sink_arg in sinks:
            for sink in sink_arg:
                for annotation in sink.annotations:
                    sink_annotations.add(annotation)
        return sink_annotations

    @staticmethod
    def gen_taint_analysis_report(sources, sinks, jni_method_signature):
        """
        Generate the taint analysis report
        :param sources: Sources annotation
        :param sinks: Sinks annotation
        :param jni_method_signature: JNI method signature
        :return: taint analysis report
        """
        report_file = StringIO()
        report_file.write(jni_method_signature)
        if sinks:
            report_file.write(' -> _SINK_ ')
            for sink_annotation in sinks:
                if not sink_annotation.field_info['is_field']:
                    if sink_annotation.source.startswith('arg'):
                        sink_location = re.split('arg|_', sink_annotation.source)[1]
                        report_file.write(str(sink_location))
                    else:
                        report_file.write(annotation_location[sink_annotation.source])

        elif sources:
            report_file.write(' -> _SOURCE_ ')
            for source_annotation in sources:
                if type(source_annotation) is JobjectAnnotation and source_annotation.source.startswith('arg'):
                    source_location = source_annotation.source
                    taint_field_name = None
                    for field_info in source_annotation.fields_info:
                        taint_field_name = field_info.field_info['field_name']
                        if field_info.taint_info['is_taint'] and field_info.taint_info['taint_type'][0] != '_ARGUMENT_':
                            taint_field_name = field_info.field_info['field_name']
                        else:
                            if type(field_info) is JobjectAnnotation:
                                for info in field_info.fields_info:
                                    if info.taint_info['is_taint'] and \
                                            info.taint_info['taint_type'][0] != '_ARGUMENT_':
                                        taint_field_name = taint_field_name + '.' + info.field_info['field_name']
                    if taint_field_name:
                        report_file.write(source_location.split('arg')[-1] + '.' + taint_field_name)
                    elif source_annotation.taint_info['taint_type'][1] != '_ARGUMENT_':
                        report_file.write(annotation_location[source_annotation.source])
                elif type(source_annotation) is JstringAnnotation and source_annotation.source.startswith('arg'):
                    if source_annotation.taint_info['is_taint'] and \
                            source_annotation.taint_info['taint_type'][1] != '_ARGUMENT_':
                        report_file.write(annotation_location[source_annotation.source])

        return report_file.getvalue()

    def gen_saf_summary_report(self, jni_method_signature):
        """
        Generate SAF summary report
        :param jni_method_signature:
        :return: summary report
        """
        args_safsu = dict()
        rets_safsu = list()

        for arg_index, arg_summary in self._arguments_summary.iteritems():
            arg_safsu = dict()
            for annotation in arg_summary.annotations:
                if type(annotation) is JobjectAnnotation and annotation.fields_info:
                    for field_info in annotation.fields_info:
                        field_name = field_info.field_info['field_name']
                        field_type = field_info.obj_type.replace('/', '.')
                        field_locations = list()
                        if field_info.source in annotation_location:
                            field_location = annotation_location[field_info.source]
                            field_locations.append(field_location)
                        elif field_info.source.startswith('arg'):
                            field_location = 'arg:' + str(re.split('arg|_', field_info.source)[1])
                            field_locations.append(field_location)
                        elif field_info.source == 'from_object':
                            if field_info.field_info['original_subordinate_obj'].startswith('arg'):
                                field_location = 'arg:' + str(
                                    re.split('arg|_', field_info.field_info['original_subordinate_obj'])[
                                        1]) + '.' + field_info.field_info['field_name']
                                field_locations.append(field_location)
                        arg_safsu[field_name] = (field_type, field_locations)
            args_safsu[arg_index] = arg_safsu

        return_nodes = list()
        for node in self.cfg.nodes():
            if not node.is_simprocedure:
                if node.block.vex.jumpkind == 'Ijk_Ret' and node.function_address == self._jni_method_addr:
                    return_nodes.append(node)
        for return_node in return_nodes:
            for final_state in return_node.final_states:
                return_value = final_state.regs.r0
                for annotation in return_value.annotations:
                    if type(annotation) is JobjectAnnotation:
                        if annotation.field_info['is_field']:
                            ret_type = annotation.obj_type.replace('/', '.')
                            ret_location = 'arg:' + str(
                                re.split('arg|_', annotation.field_info['current_subordinate_obj'])[1]
                            ) + '.' + annotation.field_info['field_name']
                            ret_safsu = '  ret = ' + ret_type + '@' + ret_location
                            rets_safsu.append(ret_safsu)
                        else:
                            ret_type = get_java_return_type(annotation.obj_type)
                            ret_location = annotation_location[annotation.source]
                            ret_safsu = '  ret = ' + ret_type + '@' + ret_location
                            rets_safsu.append(ret_safsu)
                    elif type(annotation) is JstringAnnotation:
                        # ret_type = annotation.primitive_type.split('L')[-1].replace('/', '.')
                        ret_type = 'java.lang.String'
                        ret_location = annotation_location[annotation.source]
                        ret_value = annotation.value
                        if ret_value is not None:
                            ret_safsu = '  ret = ' + ret_type + '@' + ret_location + '(' + ret_value + ')'
                        else:
                            ret_safsu = '  ret = ' + ret_type + '@' + ret_location
                        rets_safsu.append(ret_safsu)

        report_file = StringIO()
        report_file.write('`' + jni_method_signature + '`:' + '\n')
        if args_safsu:
            for arg_index, fields_safsu in args_safsu.iteritems():
                arg_index = 'arg:' + str(re.split('arg|_', arg_index)[1])
                for field_name, field_safsu in fields_safsu.iteritems():
                    field_type = field_safsu[0]
                    field_locations = field_safsu[1]
                    field_safsu = arg_index + '.' + field_name + ' = ' + field_type + '@' + field_locations[0]
                    report_file.write('  ' + field_safsu.strip() + '\n')
        if rets_safsu:
            for ret_safsu in rets_safsu:
                report_file.write(ret_safsu + '\n')
        report_file.write(';\n')
        return report_file.getvalue()

    def run(self):
        """
        run the analysis.
        :return:
        """
        sources = self._collect_taint_sources()
        sinks = self._collect_taint_sinks()
        return sources, sinks
