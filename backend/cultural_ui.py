"""Embeddable ForgeForm cultural product workbench."""
import gradio as gr
from .cultural_lab import PRODUCTS, THEMES, STYLES, run, rotate_static

def _defaults(product):
    if product==PRODUCTS[0]:return 82,82,100
    if product==PRODUCTS[1]:return 130,32,2.8
    if product==PRODUCTS[3]:return 150,30,3
    return 166,32,3

def _generate(*args):
    try:return run(*args)
    except Exception as exc:return None,None,None,f'生成失败：{type(exc).__name__}：{exc}',None,None

def render():
    with gr.Accordion('🏛️ 南航灵感 · 参数化文创工作台',open=False,elem_id='cultural_lab'):
        preview_state=gr.State(None)
        gr.Markdown('选择产品与校园主题，调整尺寸、镂空、过孔和图案，再生成可预览、可下载的实体模型。尺子刻度按毫米生成。')
        with gr.Row():
            with gr.Column(scale=1):
                product=gr.Dropdown(PRODUCTS,value=PRODUCTS[0],label='产品模板')
                theme=gr.Dropdown(THEMES,value=THEMES[0],label='校园元素')
                style=gr.Dropdown(STYLES,value=STYLES[0],label='结构风格')
                with gr.Row():
                    x=gr.Number(value=82,label='长度 / 笔筒直径 (mm)')
                    y=gr.Number(value=82,label='宽度 (mm，笔筒不用)')
                    z=gr.Number(value=100,label='厚度 / 笔筒高度 (mm)')
                pattern=gr.Checkbox(value=True,label='启用镂空结构')
                with gr.Row():
                    wall=gr.Slider(2.4,5,value=3,step=.2,label='笔筒壁厚 (mm)')
                    density=gr.Dropdown([8,10,12,14],value=12,label='笔筒镂空列数')
                    logo_size=gr.Slider(.6,1.4,value=1,step=.1,label='图案大小')
                with gr.Row():
                    hole_d=gr.Slider(0,12,value=0,step=0.5,label='额外过孔直径 (mm，0 为不加)')
                    hole_pos=gr.Slider(0,1,value=.5,step=.05,label='过孔位置（沿主体长度）')
                logo=gr.Image(type='filepath',sources=['upload'],label='可选：上传白底黑色 / 透明底黑色图案')
                batch=gr.Checkbox(value=False,label='批量生成：尺寸 90% / 100% / 110%')
                btn=gr.Button('生成文创模型',variant='primary')
            with gr.Column(scale=1):
                image=gr.Image(label='实际模型静态预览（无需 3D 加速）',interactive=False,height=360)
                with gr.Row():
                    left=gr.Button('↶ 左转 45°')
                    right=gr.Button('↷ 右转 45°')
                view=gr.File(label='辅助 GLB 预览（非打印尺寸）',visible=False)
                status=gr.Markdown('生成后用左右按钮旋转实际模型预览；打印细节以切片为准。')
                with gr.Row():
                    single=gr.File(label='当前 STL')
                    archive=gr.File(label='批量模型 ZIP（含参数）')
        product.change(_defaults,inputs=[product],outputs=[x,y,z],api_name=False)
        btn.click(_generate,inputs=[product,theme,x,y,z,hole_d,hole_pos,pattern,logo,batch,style,wall,density,logo_size],outputs=[view,single,archive,status,image,preview_state],api_name='cultural_generate')
        left.click(lambda s:rotate_static(s,-1),inputs=[preview_state],outputs=[image,preview_state],api_name=False)
        right.click(lambda s:rotate_static(s,1),inputs=[preview_state],outputs=[image,preview_state],api_name=False)
