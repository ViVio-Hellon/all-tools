/**
 * 平板3Dビューア
 * Three.js で平板を立体で表示する(コイルの 3d-coil-viewer.js と同じ仕組み)。
 * 寸法 A・B・板厚 を図と同じ色の札で示す(A 赤・B 青・板厚 緑)。
 */
class PlateViewer3D {
  /**
   * @param {string} containerId - 3Dビューを表示するコンテナのID
   * @param {object} options - 設定オプション
   */
  constructor(containerId, options = {}) {
    this.container = document.getElementById(containerId);
    if (!this.container) {
      console.error(`Container element with ID "${containerId}" not found.`);
      return;
    }

    this.options = {
      backgroundColor: 0xf5f5f5,
      plateColor: 0xaaaaaa,
      autoRotate: true,
      autoRotateSpeed: 1.0,
      ...options
    };

    // 板厚は実物の比だと線にしか見えないので、長い辺のこの割合より薄いときは厚く描く
    this.MIN_VISIBLE_RATIO = 0.015;

    this.initThreeJS();
    window.addEventListener('resize', this.onWindowResize.bind(this));
    this.animate();
  }

  /**
   * Three.jsの初期化
   */
  initThreeJS() {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    this.renderer.setSize(this.container.offsetWidth || 1, this.container.offsetHeight || 1);
    this.renderer.setClearColor(this.options.backgroundColor);
    this.container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(this.options.backgroundColor);

    this.camera = new THREE.PerspectiveCamera(
      45, (this.container.offsetWidth || 1) / (this.container.offsetHeight || 1), 0.1, 10000
    );
    this.camera.position.set(900, 700, 900);
    this.camera.lookAt(0, 0, 0);

    // ライト(コイルと同じ)
    this.scene.add(new THREE.AmbientLight(0x404040, 0.6));
    const light1 = new THREE.DirectionalLight(0xffffff, 0.8);
    light1.position.set(1, 1, 1).normalize();
    this.scene.add(light1);
    const light2 = new THREE.DirectionalLight(0xffffff, 0.5);
    light2.position.set(-1, 0.5, -1).normalize();
    this.scene.add(light2);
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 0.2));

    this.plateGroup = new THREE.Group();
    this.scene.add(this.plateGroup);

    this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.05;
    this.controls.screenSpacePanning = false;
    this.controls.maxPolarAngle = Math.PI / 1.5;
    this.controls.target.set(0, 0, 0);
    this.controls.autoRotate = this.options.autoRotate;
    this.controls.autoRotateSpeed = this.options.autoRotateSpeed;

    this.gridHelper = null;
    this.setupLabels();
  }

  /**
   * 情報表示用ラベル(コイルと同じ見た目)
   */
  setupLabels() {
    this.infoDiv = document.createElement('div');
    this.infoDiv.className = 'plate-viewer-info';
    this.infoDiv.style.display = 'block';   // コイルと同じく、最初は出しておく
    this.container.appendChild(this.infoDiv);
  }

  /**
   * 平板モデルの更新
   * @param {number} a - A (mm)
   * @param {number} b - B (mm)
   * @param {number} t - 板厚 (mm)
   */
  updatePlateModel(a, b, t) {
    this.lastSize = { a, b, t };

    // 既存のモデルを削除
    while (this.plateGroup.children.length > 0) {
      const child = this.plateGroup.children[0];
      this.plateGroup.remove(child);
      this.disposeObject(child);
    }

    // いちばん長い寸法を 1000 に正規化(コイルと同じ)
    const maxDimension = Math.max(a, b, t, 1);   // 板厚は最大 1000mm まで入るので含める
    const scale = 1000 / maxDimension;
    const sa = Math.max(a * scale, 1);
    const sb = Math.max(b * scale, 1);
    const realT = t * scale;
    const minT = 1000 * this.MIN_VISIBLE_RATIO;
    const st = Math.max(realT, minT);
    this.exaggerated = realT < minT;

    // 板(B を X 方向、A を Z 方向、板厚を Y 方向)
    const geometry = new THREE.BoxGeometry(sb, st, sa);
    const material = new THREE.MeshPhongMaterial({ color: this.options.plateColor, shininess: 60 });
    this.plateMesh = new THREE.Mesh(geometry, material);
    this.plateGroup.add(this.plateMesh);

    // 縁の線
    const edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(geometry),
      new THREE.LineBasicMaterial({ color: 0x555555 })
    );
    this.plateGroup.add(edges);

    // 寸法線と札
    this.addDimensions(sa, sb, st, a, b, t);

    this.updateGrid(st, Math.max(sa, sb));
    this.updateInfoPanel(a, b, t);
    this.adjustCamera(1000);

    document.dispatchEvent(new CustomEvent('plate-exaggerated', { detail: { exaggerated: this.exaggerated } }));
  }

  /**
   * 寸法線(A 赤・B 青・板厚 緑)
   */
  addDimensions(sa, sb, st, a, b, t) {
    const top = st / 2;
    const gap = 60;
    const colors = { a: '#b3121f', b: '#1030c8', t: '#128a2a' };

    // B: 奥の辺の上(X 方向)
    this.addDimLine(
      new THREE.Vector3(-sb / 2, top + gap, -sa / 2),
      new THREE.Vector3(sb / 2, top + gap, -sa / 2),
      colors.b, `B  ${b} mm`
    );
    // A: 左の辺の外(Z 方向)
    this.addDimLine(
      new THREE.Vector3(-sb / 2 - gap, top + gap * 0.5, -sa / 2),
      new THREE.Vector3(-sb / 2 - gap, top + gap * 0.5, sa / 2),
      colors.a, `A  ${a} mm`
    );
    // 板厚: 手前右の角
    this.addDimLine(
      new THREE.Vector3(sb / 2 + gap * 0.6, -st / 2, sa / 2),
      new THREE.Vector3(sb / 2 + gap * 0.6, st / 2, sa / 2),
      colors.t, `板厚  ${t} mm`, true
    );
  }

  addDimLine(from, to, color, text, labelBeside = false) {
    const material = new THREE.LineBasicMaterial({ color });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([from, to]), material);
    this.plateGroup.add(line);

    // 端の短い線
    const dir = new THREE.Vector3().subVectors(to, from).normalize();
    const side = Math.abs(dir.y) > 0.9 ? new THREE.Vector3(1, 0, 0) : new THREE.Vector3(0, 1, 0);
    [from, to].forEach(p => {
      const tick = new THREE.Line(new THREE.BufferGeometry().setFromPoints([
        p.clone().addScaledVector(side, -12), p.clone().addScaledVector(side, 12)
      ]), material);
      this.plateGroup.add(tick);
    });

    const sprite = this.makeLabel(text, color);
    const mid = new THREE.Vector3().addVectors(from, to).multiplyScalar(0.5);
    if (labelBeside) mid.x += 120;
    else mid.y += 45;
    sprite.position.copy(mid);
    this.plateGroup.add(sprite);
  }

  /**
   * 札(図と同じ、色地に白文字)
   */
  makeLabel(text, color) {
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');
    const font = 'bold 44px "Segoe UI", "Meiryo", sans-serif';
    ctx.font = font;
    const w = Math.ceil(ctx.measureText(text).width) + 36;
    canvas.width = w;
    canvas.height = 68;
    ctx.font = font;
    ctx.fillStyle = color;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#ffffff';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 18, canvas.height / 2 + 2);
    const texture = new THREE.CanvasTexture(canvas);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, depthTest: false }));
    sprite.renderOrder = 10;
    const h = 60;
    sprite.scale.set(h * canvas.width / canvas.height, h, 1);
    return sprite;
  }

  disposeObject(obj) {
    if (obj.geometry) obj.geometry.dispose();
    if (obj.material) {
      if (obj.material.map) obj.material.map.dispose();
      obj.material.dispose();
    }
  }

  /**
   * グリッドを板の下に置く
   */
  updateGrid(st, size) {
    if (this.gridHelper) {
      this.scene.remove(this.gridHelper);
      this.disposeObject(this.gridHelper);
    }
    this.gridHelper = new THREE.GridHelper(Math.max(2000, size * 2), 20, 0x444444, 0x444444);
    this.gridHelper.position.y = -st / 2 - 1;
    this.gridHelper.material.opacity = 0.2;
    this.gridHelper.material.transparent = true;
    this.scene.add(this.gridHelper);
  }

  adjustCamera(objectSize) {
    const distance = objectSize * 1.2;
    this.camera.position.set(distance * 0.9, distance * 0.75, distance);
    this.camera.lookAt(0, 0, 0);
    this.controls.target.set(0, 0, 0);
    this.controls.minDistance = objectSize * 0.5;
    this.controls.maxDistance = objectSize * 4.0;
    this.controls.update();
  }

  updateInfoPanel(a, b, t) {
    const rows = [`A: ${a} mm`, `B: ${b} mm`, `板厚: ${t.toFixed(2)} mm`];
    if (this.exaggerated) rows.push('(板厚は厚く描いています)');
    this.infoDiv.replaceChildren(...rows.map(text => {
      const div = document.createElement('div');
      div.textContent = text;
      return div;
    }));
  }

  toggleInfo() {
    this.infoDiv.style.display = this.infoDiv.style.display === 'none' ? 'block' : 'none';
  }

  onWindowResize() {
    if (!this.container || !this.container.offsetWidth) return;
    this.camera.aspect = this.container.offsetWidth / this.container.offsetHeight;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(this.container.offsetWidth, this.container.offsetHeight);
  }

  /**
   * アニメーションループ(見えていないあいだは描かない)
   */
  animate() {
    requestAnimationFrame(this.animate.bind(this));
    if (this.container.offsetParent === null) return;
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  setAutoRotate(enabled) {
    this.controls.autoRotate = enabled;
  }

  setBackgroundColor(colorHex) {
    const color = new THREE.Color(colorHex);
    this.scene.background = color;
    this.renderer.setClearColor(color);
  }

  setPlateColor(colorHex) {
    this.options.plateColor = new THREE.Color(colorHex);
    if (this.plateMesh) this.plateMesh.material.color = this.options.plateColor;
  }

  resetCamera() {
    this.adjustCamera(1000);
  }
}
