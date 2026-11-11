import { AbilityConstant, UIAbility, Want } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { Prefs } from '../model/Prefs';
import { UploadManager } from '../model/UploadManager';

export default class EntryAbility extends UIAbility {
  onCreate(want: Want, launchParam: AbilityConstant.LaunchParam): void {
  }

  onDestroy(): void {
  }

  onWindowStageCreate(windowStage: window.WindowStage): void {
    Prefs.init(this.context);
    UploadManager.instance().setContext(this.context);
    UploadManager.instance().setBaseUrl(Prefs.getBase());
    windowStage.loadContent('pages/Index', (err) => {
      if (err.code) {
        console.error('[yima] loadContent failed: ' + JSON.stringify(err));
      }
    });
  }

  onForeground(): void {
  }

  onBackground(): void {
  }
}
