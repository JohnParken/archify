export const DIAGRAM_TYPES = Object.freeze([
  'architecture',
  'workflow',
  'sequence',
  'dataflow',
  'lifecycle',
]);

export const DIAGRAM_TYPE_LABELS = Object.freeze({
  en: Object.freeze({
    architecture: 'Architecture',
    workflow: 'Workflow',
    sequence: 'Sequence',
    dataflow: 'Data flow',
    lifecycle: 'Lifecycle',
  }),
  zh: Object.freeze({
    architecture: '架构图',
    workflow: '工作流',
    sequence: '时序图',
    dataflow: '数据流',
    lifecycle: '生命周期',
  }),
});
