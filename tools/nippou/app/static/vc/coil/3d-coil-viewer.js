/**
 * アルミニウムコイル3Dビューア
 * Three.jsを使用してコイルの3Dモデルを表示する機能
 */
class CoilViewer3D {
  /**
   * 3Dビューアの初期化
   * @param {string} containerId - 3Dビューを表示するコンテナのID
   * @param {object} options - 設定オプション
   */
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    if (!this.container) {
      console.error(`Container element with ID "${containerId}" not found.`);
      return;
    }

    // デフォルトオプション
    this.options = {
      backgroundColor: 0xf5f5f5,
      coilColor: 0xaaaaaa,
      ambientLightColor: 0x404040,
      directionalLightColor: 0xffffff,
      autoRotate: true,
      autoRotateSpeed: 1.0,
      ...options
    };
    
    // 材質カラー対応表 - 将来的に拡張しやすいよう設定
    this.materialColors = {
      aluminum: 0xAAAAAA,  // アルミニウム色
      steel: 0x71797E,     // 鉄色
      copper: 0xB87333,    // 銅色
      stainless: 0xC0C0C0  // ステンレス色
    };

    // Three.jsの初期化
    this.initThreeJS();
    
    // イベントリスナー
    window.addEventListener('resize', this.onWindowResize.bind(this));
    
    // 初期アニメーション
    this.animate();
    
    // 初期モデルを表示
    this.updateCoilModel(400, 557, 1250, 1.0);
  }

  /**
   * Three.jsの初期化
   */
  initThreeJS() {
    // レンダラー
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    this.renderer.setSize(this.container.offsetWidth, this.container.offsetHeight);
    this.renderer.setClearColor(this.options.backgroundColor);
    this.renderer.shadowMap.enabled = true;
    this.container.appendChild(this.renderer.domElement);

    // シーン
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(this.options.backgroundColor);

    // カメラ
    this.camera = new THREE.PerspectiveCamera(
      45, this.container.offsetWidth / this.container.offsetHeight, 0.1, 5000
    );
    
    // 初期カメラ位置
    this.camera.position.set(1500, 1000, 1000);
    this.camera.lookAt(0, 0, 0);

    // ライト
    this.setupLights();

    // コイルグループ
    this.coilGroup = new THREE.Group();
    this.scene.add(this.coilGroup);

    // コントロール
    this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.05;
    this.controls.screenSpacePanning = false;
    this.controls.minDistance = 300;
    this.controls.maxDistance = 4000;
    this.controls.maxPolarAngle = Math.PI / 1.5;
    this.controls.target.set(0, 0, 0);
    this.controls.autoRotate = this.options.autoRotate;
    this.controls.autoRotateSpeed = this.options.autoRotateSpeed;
    
    // グリッド（床）は後でコイルサイズに合わせて配置するため、ここではまだ追加しない
    this.gridHelper = new THREE.GridHelper(2000, 20, 0x444444, 0x444444);
    this.gridHelper.material.opacity = 0.2;
    this.gridHelper.material.transparent = true;
    
    // 座標軸ヘルパー（参考用）
    const axesHelper = new THREE.AxesHelper(500);
    this.scene.add(axesHelper);

    // 情報表示用のラベル
    this.setupLabels();
  }

  /**
   * ライトの設定
   */
  setupLights() {
    // 環境光
    const ambientLight = new THREE.AmbientLight(this.options.ambientLightColor, 0.6);
    this.scene.add(ambientLight);

    // 平行光（太陽光のように一定方向から当たる光）
    const directionalLight1 = new THREE.DirectionalLight(this.options.directionalLightColor, 0.8);
    directionalLight1.position.set(1, 1, 1).normalize();
    this.scene.add(directionalLight1);

    // 2つ目の光源（反対側から）
    const directionalLight2 = new THREE.DirectionalLight(this.options.directionalLightColor, 0.5);
    directionalLight2.position.set(-1, 0.5, -1).normalize();
    this.scene.add(directionalLight2);

    // 半球光（空と地面からの光をシミュレート）
    const hemisphereLight = new THREE.HemisphereLight(0xffffff, 0x444444, 0.2);
    this.scene.add(hemisphereLight);
  }

  /**
   * 情報表示用ラベルの設定
   */
  setupLabels() {
    this.infoDiv = document.createElement('div');
    this.infoDiv.className = 'coil-viewer-info';
    this.infoDiv.style.position = 'absolute';
    this.infoDiv.style.top = '10px';
    this.infoDiv.style.left = '10px';
    this.infoDiv.style.color = '#333';
    this.infoDiv.style.backgroundColor = 'rgba(255, 255, 255, 0.7)';
    this.infoDiv.style.padding = '10px';
    this.infoDiv.style.borderRadius = '5px';
    this.infoDiv.style.fontSize = '14px';
    this.infoDiv.style.fontWeight = 'bold';
    this.infoDiv.style.fontFamily = 'Arial, sans-serif';
    this.infoDiv.style.display = 'none';
    this.container.appendChild(this.infoDiv);
  }

  /**
   * コイルモデルの更新
   * @param {number} thickness - 肉厚(mm)
   * @param {number} innerDiameter - 内径(mm)
   * @param {number} coilWidth - コイル幅(mm)
   * @param {number} plateThickness - 板厚(mm)
   */
  updateCoilModel(thickness, innerDiameter, coilWidth, plateThickness) {
    console.log("コイルモデル更新:", { thickness, innerDiameter, coilWidth, plateThickness });
    
    // 既存のコイルをシーンから削除
    while (this.coilGroup.children.length > 0) {
      const child = this.coilGroup.children[0];
      this.coilGroup.remove(child);
      if (child.geometry) child.geometry.dispose();
      if (child.material) child.material.dispose();
    }

    // 寸法計算
    const outerDiameter = innerDiameter + (thickness * 2);
    const outerRadius = outerDiameter / 2;
    const innerRadius = innerDiameter / 2;
    
    // 最大寸法を基準にスケーリング係数を決定
    const maxDimension = Math.max(outerDiameter, coilWidth);
    const scaleFactor = 1000 / maxDimension; // 1000ユニットに正規化
    
    const scaledOuterRadius = outerRadius * scaleFactor;
    const scaledInnerRadius = innerRadius * scaleFactor;
    const scaledWidth = coilWidth * scaleFactor;
    
    // 巻き数
    const windings = Math.round(thickness / plateThickness);
    
    // グリッドの配置をコイルの下端に合わせる
    this.updateGridPosition(scaledOuterRadius);
    
    // トイレットペーパーのような中空コイルを作成
    this.createToiletPaperRoll(scaledOuterRadius, scaledInnerRadius, scaledWidth);
    
    // 情報表示を更新
    this.updateInfoPanel(thickness, innerDiameter, outerDiameter, coilWidth, plateThickness, windings);
    
    // カメラの位置を調整
    this.adjustCamera(maxDimension * scaleFactor);
  }
  
  /**
   * グリッドの位置をコイルの下端に合わせる
   * @param {number} outerRadius - コイルの外径 (スケーリング済み)
   */
  updateGridPosition(outerRadius) {
    // 既存のグリッドがシーンにあれば削除
    if (this.gridHelper.parent) {
      this.scene.remove(this.gridHelper);
    }
    
    // グリッドサイズはコイル径の2倍程度
    const gridSize = Math.max(2000, outerRadius * 4);
    
    // 新しいグリッドを作成
    this.gridHelper = new THREE.GridHelper(gridSize, 20, 0x444444, 0x444444);
    
    // グリッドの位置をコイルの下端に合わせる（Y軸が上下方向）
    this.gridHelper.position.y = -outerRadius;
    
    this.gridHelper.material.opacity = 0.2;
    this.gridHelper.material.transparent = true;
    this.scene.add(this.gridHelper);
  }
  
  /**
   * トイレットペーパーのような中空コイルを作成
   */
  createToiletPaperRoll(outerRadius, innerRadius, width) {
    // 外側の円筒ジオメトリ
    const cylinderGeometry = new THREE.CylinderGeometry(
      outerRadius,    // 上部の半径
      outerRadius,    // 下部の半径
      width,          // 高さ（コイル幅）
      32,             // 円周方向の分割数
      1,              // 高さ方向の分割数
      false           // オープンエンドかどうか
    );
    
    // 内側の円筒ジオメトリ（穴）
    const holeGeometry = new THREE.CylinderGeometry(
      innerRadius,    // 上部の半径
      innerRadius,    // 下部の半径
      width + 1,      // 少し長めにして確実に穴を開ける
      32,             // 円周方向の分割数
      1,              // 高さ方向の分割数
      false           // オープンエンドかどうか
    );
    
    // 横向きにするために回転（X軸に沿って配置）
    cylinderGeometry.rotateZ(Math.PI / 2);
    holeGeometry.rotateZ(Math.PI / 2);
    
    // コイルのマテリアル
    const coilMaterial = new THREE.MeshPhongMaterial({
      color: this.options.coilColor,
      side: THREE.DoubleSide,
      shininess: 50
    });
    
    // ThreeBSPが利用可能な場合はCSGを使用
    if (typeof THREE.CSG !== 'undefined') {
      const cylinderMesh = new THREE.Mesh(cylinderGeometry);
      const holeMesh = new THREE.Mesh(holeGeometry);
      
      const coilCSG = new THREE.CSG().union([cylinderMesh]);
      const holeCSG = new THREE.CSG().union([holeMesh]);
      const hollowCoil = coilCSG.subtract(holeCSG).toMesh();
      
      hollowCoil.material = coilMaterial;
      this.coilGroup.add(hollowCoil);
    } else {
      // CSG利用不可の場合は両方を表示して穴っぽく見せる
      const cylinderMesh = new THREE.Mesh(cylinderGeometry, coilMaterial);
      this.coilGroup.add(cylinderMesh);
      
      // 穴を表現する黒い円筒 - マットな質感に修正
      const holeMaterial = new THREE.MeshStandardMaterial({
        color: 0x0a0a0a,  // 非常に暗い灰色（純黒よりも自然）
        roughness: 1.0,   // 完全にマットな質感（最大粗さ）
        metalness: 0.0,   // 金属感なし
        side: THREE.DoubleSide,
        emissive: 0x000000, // 発光なし
        flatShading: true,  // フラットシェーディング
        transparent: true,  // 透明度を有効化
        opacity: 0.97       // わずかに透明に
      });
      
      const holeMesh = new THREE.Mesh(holeGeometry, holeMaterial);
      // 穴の円筒をほんのわずかに大きくして重なりを解消（デプスファイティング対策）
      holeMesh.scale.set(1.005, 1.005, 1.005);
      this.coilGroup.add(holeMesh);
    }
    
    // 断面の円環を作成して端面を表現
    this.addEndRings(innerRadius, outerRadius, width);
  }
  
  /**
   * 端面の円環を追加
   */
  addEndRings(innerRadius, outerRadius, width) {
    const ringGeometry = new THREE.RingGeometry(
      innerRadius, outerRadius, 32, 1
    );
    
    const endMaterial = new THREE.MeshPhongMaterial({
      color: this.options.coilColor,
      side: THREE.DoubleSide
    });
    
    // 左端の円環
    const leftRing = new THREE.Mesh(ringGeometry, endMaterial);
    leftRing.position.x = -width / 2;
    leftRing.rotation.y = Math.PI / 2;
    
    // 右端の円環
    const rightRing = new THREE.Mesh(ringGeometry, endMaterial);
    rightRing.position.x = width / 2;
    rightRing.rotation.y = Math.PI / 2;
    
    // グループに追加
    this.coilGroup.add(leftRing);
    this.coilGroup.add(rightRing);
    
    // 内側の黒い部分の端面も追加
    const innerEndMaterial = new THREE.MeshStandardMaterial({
      color: 0x0a0a0a,  // 非常に暗い灰色（純黒よりも自然）
      roughness: 1.0,   // 完全にマットな質感
      metalness: 0.0,   // 金属感なし
      side: THREE.DoubleSide,
      emissive: 0x000000, // 発光なし
      flatShading: true,  // フラットシェーディング
      depthWrite: true,   // 深度バッファへの書き込みを有効に
      polygonOffset: true, // ポリゴンオフセットを有効化
      polygonOffsetFactor: 1, // オフセット係数
      polygonOffsetUnits: 1   // オフセット単位
    });
    
    // 左側の内部円 - わずかにオフセット
    const innerLeftCircle = new THREE.Mesh(
      new THREE.CircleGeometry(innerRadius, 32),
      innerEndMaterial
    );
    innerLeftCircle.position.x = -width / 2 - 0.05; // ほんのわずかに前に出す
    innerLeftCircle.rotation.y = Math.PI / 2;
    this.coilGroup.add(innerLeftCircle);
    
    // 右側の内部円 - わずかにオフセット
    const innerRightCircle = new THREE.Mesh(
      new THREE.CircleGeometry(innerRadius, 32),
      innerEndMaterial
    );
    innerRightCircle.position.x = width / 2 + 0.05; // ほんのわずかに前に出す
    innerRightCircle.rotation.y = -Math.PI / 2;
    this.coilGroup.add(innerRightCircle);
  }

  /**
   * カメラ位置を調整
   */
  adjustCamera(objectSize) {
    // 適切な距離を計算
    const distance = objectSize * 1.5;
    
    // カメラを見やすい位置に移動
    this.camera.position.set(distance, distance * 0.5, distance);
    this.camera.lookAt(0, 0, 0);
    
    // コントロール範囲も調整
    this.controls.minDistance = objectSize * 1.0;
    this.controls.maxDistance = objectSize * 3.0;
    
    // コントロールを更新
    this.controls.update();
  }

  /**
   * 情報パネルの更新
   */
  updateInfoPanel(thickness, innerDiameter, outerDiameter, coilWidth, plateThickness, windings) {
    this.infoDiv.innerHTML = `
      <div>肉厚: ${thickness} mm</div>
      <div>内径: ${innerDiameter} mm</div>
      <div>外径: ${outerDiameter} mm</div>
      <div>幅: ${coilWidth} mm</div>
      <div>板厚: ${plateThickness.toFixed(2)} mm</div>
      <div>巻数: ${windings} 巻</div>
    `;
    this.infoDiv.style.display = 'block';
  }

  /**
   * ウィンドウリサイズ時の処理
   */
  onWindowResize() {
    if (!this.container) return;
    
    this.camera.aspect = this.container.offsetWidth / this.container.offsetHeight;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(this.container.offsetWidth, this.container.offsetHeight);
  }

  /**
   * アニメーションループ
   */
  animate() {
    requestAnimationFrame(this.animate.bind(this));
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  /**
   * 自動回転の切り替え
   * @param {boolean} enabled - 自動回転を有効にするかどうか
   */
  setAutoRotate(enabled) {
    this.controls.autoRotate = enabled;
  }

  /**
   * 背景色の変更
   * @param {string} colorHex - 16進数カラーコード
   */
  setBackgroundColor(colorHex) {
    const color = new THREE.Color(colorHex);
    this.scene.background = color;
    this.renderer.setClearColor(color);
  }

  /**
   * コイルの色変更
   * @param {string} colorHex - 16進数カラーコード
   */
  setCoilColor(colorHex) {
    this.options.coilColor = new THREE.Color(colorHex);
    this.coilGroup.children.forEach(child => {
      if (child.material && child.material.color && child.material.color.getHex() !== 0x000000 && 
          child.material.color.getHex() !== 0x0a0a0a) {
        // 黒い部分（穴）以外の色を変更
        child.material.color = this.options.coilColor;
      }
    });
  }

  /**
   * 材質に合わせてコイルの色を変更
   * @param {string} materialType - 材質タイプ（キー）
   */
  setMaterialColor(materialType) {
    if (this.materialColors[materialType]) {
      this.setCoilColor(this.materialColors[materialType]);
    }
  }

  /**
   * カメラの位置をリセット
   */
  resetCamera() {
    const thickness = parseFloat(document.getElementById('thickness').value);
    const innerDiameter = parseFloat(document.getElementById('inner-diameter').value);
    const coilWidth = parseFloat(document.getElementById('coil-width').value);
    
    const outerDiameter = innerDiameter + (thickness * 2);
    const maxDimension = Math.max(outerDiameter, coilWidth);
    const scaleFactor = 1000 / maxDimension;
    
    this.adjustCamera(maxDimension * scaleFactor);
  }
}