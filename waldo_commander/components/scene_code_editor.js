import NativeEditor from "wc-native-codemirror/codemirror.js";
import * as CM from "nicegui-codemirror";

export default {
  extends: NativeEditor,
  props: { sceneHistory: {type: Array, default: () => []} },
  async mounted() {
    const view = await this.editorPromise;
    view.dispatch({effects: CM.StateEffect.appendConfig.of(
      CM.EditorState.transactionExtender.of(tr => !this.emitting && tr.docChanged
        ? {annotations: CM.isolateHistory.of("full")} : null)
    )});
    const history = this.sceneHistory;
    this.$emit("scene-mounted");
    if (!history.length || history.at(-1)[1] !== view.state.doc.toString()) return;
    // An unopened panel has no EditorView to receive the scene's transactions.
    this.emitting = false;
    view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: history[0][0]}, annotations: CM.Transaction.addToHistory.of(false)});
    for (const [, after] of history) this.setEditorValue(after);
    this.setDecorations(this.decorations);
    this.applyDiagnostics(this.diagnostics);
    this.setLineTooltips(this.lineTooltips);
    this.applyLineAnchors(this.lineAnchors);
    this.emitting = true;
  },
  beforeUnmount() {
    this.$emit("scene-unmounted");
  },
};
