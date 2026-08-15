import {
  ILayoutRestorer,
  JupyterFrontEnd,
  JupyterFrontEndPlugin
} from '@jupyterlab/application';
import { MainAreaWidget, WidgetTracker } from '@jupyterlab/apputils';
import { PageConfig } from '@jupyterlab/coreutils';
import { IDocumentManager } from '@jupyterlab/docmanager';
import { NotebookActions, NotebookPanel } from '@jupyterlab/notebook';
import { Cell } from '@jupyterlab/cells';
import { Widget } from '@lumino/widgets';

const COMMAND_ID = 'desktop-widget:open';
const NAMESPACE = 'desktop-widget';
const DEFAULT_NOTEBOOK = 'notebooks/demo.ipynb';

const startupFlag = (name: string, defaultValue = true): boolean => {
  const params = new URLSearchParams(window.location.search);
  const raw = params.get(name);
  if (raw === null) {
    return defaultValue;
  }

  const normalized = raw.trim().toLowerCase();
  return !['0', 'false', 'off', 'no'].includes(normalized);
};

class DesktopContent extends Widget {
  constructor() {
    super();
    this.addClass('jp-DesktopWidget');
    this.node.style.height = '100%';

    const iframe = document.createElement('iframe');
    iframe.className = 'jp-DesktopWidget-frame';
    iframe.src = `${PageConfig.getBaseUrl()}desktop`;
    iframe.setAttribute('title', 'Desktop');
    iframe.setAttribute('allow', 'clipboard-read; clipboard-write');
    iframe.style.width = '100%';
    iframe.style.height = '100%';
    iframe.style.border = '0';

    this.node.appendChild(iframe);
  }
}

const plugin: JupyterFrontEndPlugin<void> = {
  id: 'desktop-widget:plugin',
  autoStart: true,
  requires: [ILayoutRestorer, IDocumentManager],
  activate: (
    app: JupyterFrontEnd,
    restorer: ILayoutRestorer,
    docManager: IDocumentManager
  ) => {
    const tracker = new WidgetTracker<MainAreaWidget<DesktopContent>>({
      namespace: NAMESPACE
    });
    let widget: MainAreaWidget<DesktopContent> | null = null;

    const openWidget = async () => {
      if (widget === null || widget.isDisposed) {
        widget = new MainAreaWidget({ content: new DesktopContent() });
        widget.id = 'desktop-widget';
        widget.title.label = 'Desktop';
        widget.title.closable = true;
        await tracker.add(widget);
      }

      if (!widget.isAttached) {
        app.shell.add(widget, 'main', { mode: 'split-right' });
      }

      app.shell.activateById(widget.id);
      return widget;
    };

    const autoRunNotebookCell = async (panel: NotebookPanel) => {
      try {
        await panel.revealed;
        await panel.context.ready;
        await panel.sessionContext.ready;

        const firstCodeCellIndex = panel.content.widgets.findIndex(
          (cell: Cell) => cell.model.type === 'code'
        );

        if (firstCodeCellIndex === -1) {
          return;
        }

        panel.content.activeCellIndex = firstCodeCellIndex;
        await NotebookActions.run(panel.content, panel.sessionContext);
      } catch (error) {
        console.error(
          `Failed to auto-run the first code cell in ${DEFAULT_NOTEBOOK}`,
          error
        );
      }
    };

    const openNotebook = async () => {
      try {
        const widget = await docManager.openOrReveal(DEFAULT_NOTEBOOK, 'Notebook');
        if (widget instanceof NotebookPanel) {
          void autoRunNotebookCell(widget);
        }
        return widget;
      } catch (error) {
        console.error(`Failed to open ${DEFAULT_NOTEBOOK}`, error);
        return null;
      }
    };

    void restorer.restore(tracker, {
      command: COMMAND_ID,
      name: () => 'desktop'
    });

    app.commands.addCommand(COMMAND_ID, {
      label: 'Open Desktop',
      execute: openWidget
    });

    void app.restored.then(async () => {
      const autoRun = startupFlag('autoRunUI', true);
      const autoOpenDesktop = startupFlag('autoOpenDesktop', true);
      const autoCollapseLeft = startupFlag('autoCollapseLeft', true);

      if (autoCollapseLeft) {
        void app.commands.execute('application:toggle-left-area');
      }

      if (autoRun) {
        await openNotebook();
      }

      if (autoOpenDesktop) {
        await app.commands.execute(COMMAND_ID);
      }
    });
  }
};

export default plugin;
