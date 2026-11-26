"""NUAA-inspired cultural product geometry, independently of AI/GPU."""
from __future__ import annotations
import json, math, tempfile, zipfile
from pathlib import Path
import numpy as np
import trimesh
from shapely.geometry import Polygon, Point, box
from shapely.affinity import scale, translate, rotate
from shapely.ops import unary_union

THEMES=('奋飞园·水上飞机','明故宫·太庙井','航空航天馆·穹顶')
PRODUCTS=('翼骨镂空笔筒','飞翼镂空书签','航迹刻度尺','长征火箭书签')
STYLES=('翼肋流线','穹顶拱券','简洁条孔')

def rounded(w,h,r):
    return box(-w/2+r,-h/2+r,w/2-r,h/2-r).buffer(r,quad_segs=8)

def extrude(shape,height,position=(0,0,0)):
    if shape.geom_type=='MultiPolygon':
        return trimesh.util.concatenate([extrude(x,height,position) for x in shape.geoms if x.area>0.01])
    m=trimesh.creation.extrude_polygon(shape,height,engine='earcut');m.apply_translation(position);return m

def theme_shape(theme):
    if theme==THEMES[0]:
        # Stylised flying boat: independent original linework inspired by AG600.
        return Polygon([(-12,-1),(-3,1),(-1,8),(1,8),(3,1),(12,-1),(12,-3),(3,-2),(2,-7),(-2,-7),(-3,-2),(-12,-3)])
    if theme==THEMES[1]:
        # Well curb and opening, interpreted geometrically, not a reproduction.
        return Point(0,0).buffer(9,quad_segs=20).difference(Point(0,0).buffer(5.8,quad_segs=20))
    if theme==THEMES[2]:
        # Museum's arched roof, as a silhouette with open entrance.
        outer=Point(0,0).buffer(9,quad_segs=20).intersection(box(-10,0,10,10))
        return unary_union([outer.difference(Point(0,0).buffer(6.3,quad_segs=20)),box(-8.8,-2,8.8,.3)])
    raise ValueError('请选择主题')

def uploaded_shape(path):
    import cv2
    from PIL import Image,ImageOps
    im=Image.open(path);im=ImageOps.exif_transpose(im);im.thumbnail((160,160))
    rgba=np.asarray(im.convert('RGBA'));alpha=rgba[:,:,3];gray=cv2.cvtColor(rgba[:,:,:3],cv2.COLOR_RGB2GRAY)
    mask=(alpha>80)&(gray<150) if np.any(alpha<245) else gray<150
    if mask.mean()>.70: raise ValueError('图案背景过暗；请上传白底黑图或透明底黑图')
    contours,hierarchy=cv2.findContours(mask.astype('uint8'),cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:raise ValueError('没有找到清晰图案，请使用黑白高对比图片')
    parts=[];hierarchy=hierarchy[0]
    for i,c in enumerate(contours):
        if hierarchy[i][3]!=-1 or cv2.contourArea(c)<5:continue
        shell=cv2.approxPolyDP(c,0.8,True).reshape(-1,2)
        if len(shell)<3:continue
        holes=[];j=hierarchy[i][2]
        while j!=-1:
            a=cv2.approxPolyDP(contours[j],0.8,True).reshape(-1,2)
            if len(a)>=3:holes.append(a.tolist())
            j=hierarchy[j][0]
        p=Polygon(shell,holes).buffer(0)
        if not p.is_empty:parts.append(p)
    if not parts:raise ValueError('图案太细或太小')
    shape=unary_union(parts);x0,y0,x1,y1=shape.bounds
    shape=translate(shape,xoff=-(x0+x1)/2,yoff=-(y0+y1)/2)
    return scale(shape,xfact=20/max(x1-x0,y1-y0),yfact=-20/max(x1-x0,y1-y0),origin=(0,0))

def number_shape(s):
    from matplotlib.textpath import TextPath
    shape=Polygon()
    for a in TextPath((0,0),str(s),size=3.2).to_polygons():
        p=Polygon(a)
        if p.is_valid and p.area>1e-4:shape=shape.symmetric_difference(p)
    return shape

def design(product,theme,x,y,z,hole_d,hole_pos,pattern,logo=None,style=STYLES[0],wall=3.,density=12,logo_size=1.):
    x,y,z,hole_d,hole_pos=map(float,(x,y,z,hole_d,hole_pos))
    wall=float(wall);density=int(density);logo_size=float(logo_size)
    if product not in PRODUCTS or theme not in THEMES or style not in STYLES:raise ValueError('未知产品、主题或结构风格')
    if not 0<=hole_d<=12 or not 0<=hole_pos<=1:raise ValueError('过孔参数不合法')
    if not 2.4<=wall<=5 or density not in (8,10,12,14) or not .6<=logo_size<=1.4:raise ValueError('壁厚、镂空数量或图案大小超出范围')
    if logo:
        art=uploaded_shape(logo)
    else:art=theme_shape(theme)
    if product==PRODUCTS[0]:
        # x=outer diameter, z=height; y is intentionally unused.
        if not 55<=x<=120 or not 60<=z<=150:raise ValueError('笔筒直径 55–120 mm，高度 60–150 mm')
        outer=trimesh.creation.cylinder(radius=x/2,height=z,sections=128);outer.apply_translation([0,0,z/2])
        inner=trimesh.creation.cylinder(radius=x/2-wall,height=z,sections=128);inner.apply_translation([0,0,z/2+3])
        taper=.15 if style!=STYLES[2] else 0
        for obj,inner_surface in [(outer,False),(inner,True)]:
            zz=np.clip(obj.vertices[:,2]/z,0,1)
            target=(x/2)*(1-taper+taper*zz)-(wall if inner_surface else 0)
            obj.vertices[:,:2]*=(target/(x/2-wall if inner_surface else x/2))[:,None]
        if theme==THEMES[0] and style==STYLES[0]:
            # Four low-relief fins are structural geometry, not pasted graphics.
            low=x/2*(1-taper+taper*.15);high=x/2*(1-taper+taper*.78)
            fin_shape=Polygon([(low-1,z*.15),(low+6,z*.23),(high+6,z*.55),(high-1,z*.78)])
            fins=[]
            for a in (0,math.pi/2,math.pi,3*math.pi/2):
                fin=extrude(fin_shape,3)
                T=np.array([[math.cos(a),0,-math.sin(a),1.5*math.sin(a)],
                            [math.sin(a),0,math.cos(a),-1.5*math.cos(a)],
                            [0,1,0,0],[0,0,0,1]],float)
                fin.apply_transform(T);fins.append(fin)
            outer=trimesh.boolean.union([outer]+fins,engine='manifold')
        if theme==THEMES[1]:
            # Raised stone-curb bands express the Taimao well structurally.
            collars=[]
            for lev in (.07,.93):
                ring=trimesh.creation.cylinder(radius=x/2*(1-taper+taper*lev)+1.0,height=2.4,sections=128)
                ring.apply_translation((0,0,z*lev));collars.append(ring)
            outer=trimesh.boolean.union([outer]+collars,engine='manifold')
        cuts=[inner]
        if pattern:
            # 12 radial rows of aerodynamic capsules; maintain top/bottom rims.
            for i in range(density):
                ang=2*math.pi*i/density
                for level in (.34,.62):
                    radius=x/2*(1-taper+taper*level)
                    sw=6.2 if style==STYLES[2] else min(9.0,2*math.pi*radius/density*.40)
                    sh=min(15,z*.15) if style==STYLES[2] else min(25,z*.23)
                    if style==STYLES[1]:
                        shape=unary_union([box(-sw/2,-sh/2,sw/2,sh/2-sw/2),translate(Point(0,0).buffer(sw/2,quad_segs=10),yoff=sh/2-sw/2)])
                    else:
                        shape=rounded(sw,sh,sw/2-.1)
                        if style==STYLES[0]:shape=rotate(shape,10 if i%2 else -10,origin=(0,0))
                    cutter=extrude(shape,10)
                    T=np.array([[math.cos(ang),0,-math.sin(ang),math.sin(ang)*(radius+1)],
                                [math.sin(ang),0,math.cos(ang),-math.cos(ang)*(radius+1)],
                                [0,1,0,z*level],[0,0,0,1]],float)
                    # local z is inward radial direction
                    cutter.apply_transform(T);cuts.append(cutter)
        if hole_d:
            h=trimesh.creation.cylinder(radius=hole_d/2,height=10,sections=32)
            hp=.2+.6*hole_pos;h.apply_transform(trimesh.transformations.rotation_matrix(math.pi/2,[1,0,0]));h.apply_translation((0,-x/2*(1-taper+taper*hp),z*hp));cuts.append(h)
        # Place small engraving on the unperforated band near the rim.
        art=scale(art,xfact=.5*logo_size,yfact=.5*logo_size,origin=(0,0));emb=extrude(art,1.2)
        T=np.array([[1,0,0,0],[0,0,1,-x/2*(1-taper+taper*.86)-.2],[0,1,0,z*.86],[0,0,0,1]],float);emb.apply_transform(T);cuts.append(emb)
        return trimesh.boolean.difference([outer]+cuts,engine='manifold')
    if not 80<=x<=220 or not 24<=y<=55 or not 2<=z<=6:raise ValueError('长度 80–220、宽度 24–55、厚度 2–6 mm')
    if hole_d and hole_d>y-8:raise ValueError('孔太大，边缘材料不足')
    if product==PRODUCTS[3]:
        # Rocket silhouette with structural tail fins and three portholes.
        body=Polygon([(-x*.50,0),(-x*.38,-y*.30),(x*.20,-y*.30),(x*.39,-y*.62),(x*.34,-y*.30),(x*.48,-y*.30),(x*.48,y*.30),(x*.34,y*.30),(x*.39,y*.62),(x*.20,y*.30),(-x*.38,y*.30)]).buffer(.9,join_style='round').buffer(-.9,join_style='round')
        bounds=body.bounds;body=translate(body,xoff=-(bounds[0]+bounds[2])/2)
        body=scale(body,xfact=x/(bounds[2]-bounds[0]),yfact=1,origin=(0,0))
        mesh=extrude(body,z);cutters=[]
        if pattern:
            for ox in (-.20,0,.20):
                window=Point(x*ox,0).buffer(min(3.4,y*.12),quad_segs=16)
                cutters.append(extrude(window,z+2,(0,0,-1)))
    elif product==PRODUCTS[1]:
        # Aircraft-like tapered nose and fin, joined to a sturdy central spine.
        if style==STYLES[2]:
            body=rounded(x,y,4)
            wings=Polygon([(-x*.28,-y*.35),(-x*.10,-y*.65),(x*.12,-y*.65),(x*.20,-y*.35),(x*.20,y*.35),(x*.12,y*.65),(-x*.10,y*.65),(-x*.28,y*.35)])
            body=unary_union([body,wings])
        else:
            body=Polygon([(-x*.50,0),(-x*.41,-y*.30),(-x*.24,-y*.30),(-x*.09,-y*.66),(x*.10,-y*.66),(x*.22,-y*.30),(x*.44,-y*.30),(x*.50,-y*.18),(x*.50,y*.18),(x*.44,y*.30),(x*.22,y*.30),(x*.10,y*.66),(-x*.09,y*.66),(-x*.24,y*.30),(-x*.41,y*.30)]).buffer(1.2,join_style='round').buffer(-1.2,join_style='round')
        mesh=extrude(body,z)
        cutters=[]
        if pattern:
            # Closed elliptical windows flank a solid 12 mm central spine.
            for sx in (-1,1):
                for sy in (-1,1):
                    hole=scale(Point(0,0).buffer(1,quad_segs=12),xfact=x*.095,yfact=2.6,origin=(0,0));hole=translate(hole,xoff=sx*x*.19,yoff=sy*y*.30)
                    cutters.append(extrude(hole,z+2,(0,0,-1)))
    else:
        # Scale is x-16 mm with 8 mm margins; physical tick spacing 1 mm.
        scale_len=int((x-16)//10)*10
        if scale_len<60:raise ValueError('尺子量程过短')
        mesh=extrude(rounded(x,y,2),z);cutters=[];x0=-scale_len/2
        for i in range(scale_len+1):
            length=7 if i%10==0 else 4 if i%5==0 else 2.6
            cutter=box(x0+i-.23,-y/2+1.4,x0+i+.23,-y/2+1.4+length)
            cutters.append(extrude(cutter,.8,(0,0,z-.6)))
        for i in range(0,scale_len+1,10):
            shape=number_shape(i//10)
            if not shape.is_empty:
                bx=shape.bounds;shape=translate(shape,xoff=x0+i-(bx[0]+bx[2])/2,yoff=-y/2+10-bx[1]);cutters.append(extrude(shape,.8,(0,0,z-.6)))
        if pattern and style!=STYLES[2]:
            for pos in (-.28,-.08,.12):
                window=scale(Point(0,0).buffer(1,quad_segs=12),xfact=5,yfact=1.5,origin=(0,0))
                window=translate(window,xoff=x*pos,yoff=y*.31)
                cutters.append(extrude(window,z+2,(0,0,-1)))
    if hole_d:
        # End-zone safety: placement is restricted to central 60% of length.
        hx=(hole_pos-.5)*x*.6
        h=Point(hx,0).buffer(hole_d/2,quad_segs=16);cutters.append(extrude(h,z+2,(0,0,-1)))
    art=scale(art,xfact=.5*logo_size,yfact=.5*logo_size,origin=(0,0))
    # Keep logo away from ruler markings and the customizable hole.
    art=translate(art,xoff=x*.35,yoff=y*.23 if product==PRODUCTS[2] else 0)
    cutters.append(extrude(art,.8,(0,0,z-.6)))
    return trimesh.boolean.difference([mesh]+cutters,engine='manifold')

def render_static(mesh,path,turn=0):
    """CPU painter preview of actual triangles; works without WebGL/WebGPU."""
    from PIL import Image
    vertices=np.asarray(mesh.vertices,dtype=float); center=(vertices.min(axis=0)+vertices.max(axis=0))/2
    vertices=vertices-center
    right=np.array([.82,.57,0.]);right/=np.linalg.norm(right)
    forward=np.array([.48,-.69,.54]);forward/=np.linalg.norm(forward)
    angle=turn*math.pi/4;rot=np.array([[math.cos(angle),-math.sin(angle),0],[math.sin(angle),math.cos(angle),0],[0,0,1.]])
    right=rot@right;forward=rot@forward
    up=np.cross(forward,right);up/=np.linalg.norm(up)
    screen=np.column_stack((vertices@right,vertices@up));extent=np.ptp(screen,axis=0);factor=720/max(extent.max(),1e-6)
    screen=screen*factor+np.array([450,450]);screen[:,1]=900-screen[:,1]
    faces=np.asarray(mesh.faces);depth=(vertices@forward)[faces]
    normals=np.asarray(mesh.face_normals);light=np.array([-.35,-.55,.75]);light/=np.linalg.norm(light)
    shade=np.clip(.45+.45*np.abs(normals@light),.35,.92)
    canvas=np.empty((900,900,3),dtype=np.uint8);canvas[:]=[30,42,55]
    zbuf=np.full((900,900),-np.inf,dtype=np.float32)
    for i,face in enumerate(faces):
        tri=screen[face];x0=max(0,int(np.floor(tri[:,0].min())));x1=min(899,int(np.ceil(tri[:,0].max())))
        y0=max(0,int(np.floor(tri[:,1].min())));y1=min(899,int(np.ceil(tri[:,1].max())))
        if x1<x0 or y1<y0:continue
        (ax,ay),(bx,by),(cx,cy)=tri;den=(by-cy)*(ax-cx)+(cx-bx)*(ay-cy)
        if abs(den)<1e-9:continue
        xx=np.arange(x0,x1+1,dtype=np.float32)[None,:]+.5;yy=np.arange(y0,y1+1,dtype=np.float32)[:,None]+.5
        u=((by-cy)*(xx-cx)+(cx-bx)*(yy-cy))/den
        v=((cy-ay)*(xx-cx)+(ax-cx)*(yy-cy))/den;w=1-u-v
        z=u*depth[i,0]+v*depth[i,1]+w*depth[i,2]
        region=zbuf[y0:y1+1,x0:x1+1];mask=(u>=-1e-5)&(v>=-1e-5)&(w>=-1e-5)&(z>region)
        if np.any(mask):
            region[mask]=z[mask];value=int(220*shade[i]);canvas[y0:y1+1,x0:x1+1][mask]=(value,min(value+4,255),min(value+8,255))
    Image.fromarray(canvas).save(path)

def run(product,theme,x,y,z,hole_d,hole_pos,pattern,logo,batch,style=STYLES[0],wall=3.,density=12,logo_size=1.):
    from backend.env import ROOT
    import uuid
    out=ROOT/'outputs'/'cultural_lab'/uuid.uuid4().hex[:12];out.mkdir(parents=True,exist_ok=True)
    sizes=[.9,1.,1.1] if batch else [1.]
    paths=[];report=[]
    for idx,factor in enumerate(sizes):
        xx=float(x)*factor;yy=float(y);zz=float(z)*factor if product==PRODUCTS[0] else float(z)
        mesh=design(product,theme,xx,yy,zz,hole_d,hole_pos,pattern,logo,style,wall,density,logo_size)
        path=out/f'设计_{idx+1}.stl';mesh.export(path);check=trimesh.load(path,force='mesh');pieces=check.split(only_watertight=False)
        if not check.is_watertight or not check.is_volume or len(pieces)!=1:raise ValueError('结果未通过单体水密检查，请调整孔的位置或大小')
        paths.append(path);report.append({'dimensions_mm':[round(float(t),2) for t in check.extents],'faces':len(check.faces),'watertight':check.is_watertight,'components':len(pieces)})
    chosen=paths[1] if batch else paths[0]
    scene=trimesh.load(chosen,force='mesh');static=out/'模型预览.png';render_static(scene,static)
    scene.apply_translation(-scene.bounding_box.centroid);scene.apply_scale(2/max(scene.extents));preview=out/'预览.glb';scene.export(preview)
    manifest=out/'参数与检查.json';manifest.write_text(json.dumps({'product':product,'theme':theme,'structure_style':style,'x_mm':x,'y_mm':y,'z_mm':z,'wall_mm':wall,'pattern_count':density,'logo_scale':logo_size,'hole_diameter_mm':hole_d,'hole_position':hole_pos,'pattern':pattern,'custom_logo':bool(logo),'batch_scale_factors':sizes,'results':report},ensure_ascii=False,indent=2),encoding='utf8')
    archive=out/'批量设计.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zf:
        for path in paths+[manifest]:zf.write(path,path.name)
    notes=f'已生成 {len(paths)} 款；模型导出后检查：单体、水密。实际尺寸：{report[len(paths)//2]["dimensions_mm"]} mm。'
    if product==PRODUCTS[2]:notes+=' 尺上数值单位为厘米；总长扣除两端余量后按 10 mm 取整得到量程，打印后请实测校准。'
    if logo:notes+=' 图案来自上传图片，已转换为实体刻纹。'
    return str(preview),str(chosen),str(archive),notes,str(static),[str(chosen),0]

def rotate_static(state,step):
    if not state:return None,state
    source=Path(state[0]);turn=(int(state[1])+int(step))%8
    target=source.parent/f'模型预览_{turn}.png'
    if not target.exists():render_static(trimesh.load(source,force='mesh'),target,turn)
    return str(target),[str(source),turn]
