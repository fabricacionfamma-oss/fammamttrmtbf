import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from fpdf import FPDF
import tempfile
import os

# ==========================================
# CONFIGURACIÓN DE LA PÁGINA
# ==========================================
st.set_page_config(page_title="KPIs Mantenimiento - FAMMA", layout="wide", page_icon="⚙️")

st.markdown("""
<style>
    .filter-box {
        background-color: #f0f2f6;
        padding: 20px;
        border-radius: 10px;
        border-left: 5px solid #28a745; /* Verde para diferenciar FAMMA */
        margin-bottom: 20px;
    }
</style>
""", unsafe_allow_html=True)

col_title, col_btn = st.columns([4, 1])
with col_title:
    st.title("⚙️ Análisis de MTBF, MTTR y Down Time (FAMMA)")
    st.write("Generador de Reportes PDF conectado a SQL Server (Filtro Exclusivo Matricería).")
with col_btn:
    if st.button("Limpiar Caché", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# ==========================================
# OBJETIVOS (TARGETS T y C)
# ==========================================
TARGET_DT_T = 5.2       
TARGET_DT_C = 3.0       

TARGET_MTTR_T = 30      
TARGET_MTTR_C = 20      

TARGET_MTBF_T = 600     
TARGET_MTBF_C = 500     

# ==========================================
# FILTROS DINÁMICOS
# ==========================================
st.markdown('<div class="filter-box">', unsafe_allow_html=True)
st.subheader("🔍 Filtros del Reporte")

col_f1, col_f2, col_f3 = st.columns([1, 1, 2])

with col_f1:
    anio_actual = pd.to_datetime("today").year
    anio_sel = st.selectbox("1. Seleccione el Año:", range(2023, anio_actual + 2), index=anio_actual-2023)

with col_f2:
    area_sel = st.selectbox("2. Área/Fábrica:", ["Ambas (General)", "Estampado", "Soldadura"])

with col_f3:
    meses_nombres = ['Ene', 'Feb', 'Mar', 'Abr', 'May', 'Jun', 'Jul', 'Ago', 'Sep', 'Oct', 'Nov', 'Dic']
    
    if anio_sel == anio_actual:
        mes_actual = pd.to_datetime("today").month
        default_meses = meses_nombres[:mes_actual]
    else:
        default_meses = meses_nombres
        
    meses_sel = st.multiselect(
        "3. Seleccione los Meses a incluir en el PDF:", 
        options=meses_nombres, 
        default=default_meses
    )

st.markdown('</div>', unsafe_allow_html=True)
st.divider()

if not meses_sel:
    st.warning("⚠️ Por favor, seleccione al menos un mes en el recuadro de arriba para generar el reporte.")
    st.stop()

meses_activos = [meses_nombres.index(m) + 1 for m in meses_sel]

# ==========================================
# EXTRACCIÓN Y PROCESAMIENTO DE DATOS (SQL SERVER - MATRICERÍA)
# ==========================================
@st.cache_data(ttl=300)
def fetch_annual_data_famma(anio, area_filtro):
    try:
        conn = st.connection("wii_bi", type="sql")
        
        # 1. Consulta SQL usando los 9 niveles
        q_event = f"""
            SELECT c.Name as Máquina, e.Interval as [Tiempo (Min)], e.Date as Fecha_DT,
                   t1.Name as [Nivel Evento 1], t2.Name as [Nivel Evento 2], 
                   t3.Name as [Nivel Evento 3], t4.Name as [Nivel Evento 4],
                   t5.Name as [Nivel Evento 5], t6.Name as [Nivel Evento 6],
                   t7.Name as [Nivel Evento 7], t8.Name as [Nivel Evento 8],
                   t9.Name as [Nivel Evento 9]
            FROM EVENT_01 e 
            LEFT JOIN CELL c ON e.CellId = c.CellId 
            LEFT JOIN EVENTTYPE t1 ON e.EventTypeLevel1 = t1.EventTypeId 
            LEFT JOIN EVENTTYPE t2 ON e.EventTypeLevel2 = t2.EventTypeId 
            LEFT JOIN EVENTTYPE t3 ON e.EventTypeLevel3 = t3.EventTypeId 
            LEFT JOIN EVENTTYPE t4 ON e.EventTypeLevel4 = t4.EventTypeId
            LEFT JOIN EVENTTYPE t5 ON e.EventTypeLevel5 = t5.EventTypeId
            LEFT JOIN EVENTTYPE t6 ON e.EventTypeLevel6 = t6.EventTypeId
            LEFT JOIN EVENTTYPE t7 ON e.EventTypeLevel7 = t7.EventTypeId
            LEFT JOIN EVENTTYPE t8 ON e.EventTypeLevel8 = t8.EventTypeId
            LEFT JOIN EVENTTYPE t9 ON e.EventTypeLevel9 = t9.EventTypeId
            WHERE YEAR(e.Date) = {anio}
        """
        
        df = conn.query(q_event)
        if df.empty: return pd.DataFrame()

        # 1. Limpieza de Fechas
        df['Fecha_DT'] = pd.to_datetime(df['Fecha_DT'], errors='coerce')
        df = df.dropna(subset=['Fecha_DT'])

        # 2. Asignar Fábrica
        df['Máquina'] = df['Máquina'].fillna('General')
        def get_fabrica(maq):
            m = str(maq).upper().strip()
            if "LINEA" in m or m in ["GENERAL"]: return "Estampado"
            return "Soldadura"
        
        df['Fábrica'] = df['Máquina'].apply(get_fabrica)
        
        if area_filtro != "Ambas (General)":
            df = df[df['Fábrica'] == area_filtro]

        # 3. Limpieza de Tiempo
        df['Tiempo (Min)'] = pd.to_numeric(df['Tiempo (Min)'], errors='coerce').fillna(0.0)

        # 4. Categorizar Eventos - FILTRO EXCLUSIVO MATRICERIA
        def categorizar_estado(row):
            texto = " ".join([str(row.get(f'Nivel Evento {i}', '')).upper() for i in range(1, 10)])
            
            if 'PROYECTO' in texto: return 'Proyecto'
            if any(x in texto for x in ['BAÑO', 'BANO', 'REFRIGERIO']): return 'Descanso'
            if 'PARADA PROGRAMADA' in texto: return 'Parada Programada'
            
            # SOLO los eventos de Matricería contabilizan como Falla/Gestión (Downtime)
            if 'MATRICERIA' in texto or 'MATRICERÍA' in texto:
                return 'Falla/Gestión'
            
            # Cualquier otra falla ajena a matricería o producción normal suma al tiempo productivo (Uptime) de la matriz
            return 'Producción'

        df['Estado_Global'] = df.apply(categorizar_estado, axis=1)

        # 5. Agrupar Matemáticas
        df['Mes'] = df['Fecha_DT'].dt.month
        df_meses = pd.DataFrame({'Mes': range(1, 13)})
        
        # UPTIME (Producción pura + todo el tiempo que no fue falla de matricería)
        uptime = df[df['Estado_Global'] == 'Producción'].groupby('Mes')['Tiempo (Min)'].sum().reset_index(name='Tiempo_Productivo_Min')
        
        # DOWNTIME (Fallas y Gestión - Exclusivo de Matricería)
        fallas = df[df['Estado_Global'] == 'Falla/Gestión'].groupby('Mes').agg(
            Cantidad_Fallas=('Tiempo (Min)', 'count'),
            Tiempo_Reparacion_Min=('Tiempo (Min)', 'sum')
        ).reset_index()

        df_anual = pd.merge(df_meses, uptime, on='Mes', how='left').fillna(0)
        df_anual = pd.merge(df_anual, fallas, on='Mes', how='left').fillna(0)

        # 6. Calcular Indicadores Clave
        df_anual['Uptime_Min'] = df_anual['Tiempo_Productivo_Min']
        df_anual['Downtime_Min'] = df_anual['Tiempo_Reparacion_Min']
        df_anual['Tiempo_Total_Disponible_Min'] = df_anual['Uptime_Min'] + df_anual['Downtime_Min']
        
        df_anual['DT (%)'] = df_anual.apply(lambda r: (r['Downtime_Min'] / r['Tiempo_Total_Disponible_Min'] * 100) if r['Tiempo_Total_Disponible_Min'] > 0 else 0, axis=1)
        df_anual['MTBF (Min)'] = df_anual.apply(lambda r: r['Uptime_Min'] / r['Cantidad_Fallas'] if r['Cantidad_Fallas'] > 0 else (r['Uptime_Min'] if r['Uptime_Min'] > 0 else 0), axis=1)
        df_anual['MTTR (Min)'] = df_anual.apply(lambda r: r['Downtime_Min'] / r['Cantidad_Fallas'] if r['Cantidad_Fallas'] > 0 else 0, axis=1)
        
        # 7. Acumulados (YTD)
        df_anual['Cum_Uptime'] = df_anual['Uptime_Min'].cumsum()
        df_anual['Cum_Downtime'] = df_anual['Downtime_Min'].cumsum()
        df_anual['Cum_TotalTime'] = df_anual['Tiempo_Total_Disponible_Min'].cumsum()
        df_anual['Cum_Fallas'] = df_anual['Cantidad_Fallas'].cumsum()

        df_anual['A_DT (%)'] = df_anual.apply(lambda r: (r['Cum_Downtime'] / r['Cum_TotalTime'] * 100) if r['Cum_TotalTime'] > 0 else 0, axis=1)
        df_anual['A_MTBF (Min)'] = df_anual.apply(lambda r: r['Cum_Uptime'] / r['Cum_Fallas'] if r['Cum_Fallas'] > 0 else (r['Cum_Uptime'] if r['Cum_Uptime'] > 0 else 0), axis=1)
        df_anual['A_MTTR (Min)'] = df_anual.apply(lambda r: r['Cum_Downtime'] / r['Cum_Fallas'] if r['Cum_Fallas'] > 0 else 0, axis=1)

        return df_anual
    except Exception as e:
        st.error(f"Error procesando datos de BD: {e}")
        return pd.DataFrame()

with st.spinner("Conectando con SQL Server y calculando métricas de Matricería..."):
    df_anual = fetch_annual_data_famma(anio_sel, area_sel)

# ==========================================
# GENERADOR PDF DINÁMICO (Alineación Perfecta)
# ==========================================
class ReportePD(FPDF):
    def header(self):
        self.set_font("Arial", 'B', 14)
        # Cambiamos a un color verde oscuro para diferenciar FAMMA
        self.set_text_color(34, 139, 34)
        
        area_texto = f" - {area_sel}" if area_sel != "Ambas (General)" else ""
        self.cell(0, 8, f"Reporte de Mantenimiento de Matrices FAMMA{area_texto} - Año {anio_sel}", ln=True, align='C')
        
        self.set_draw_color(34, 139, 34)
        self.set_line_width(0.5)
        self.line(10, self.get_y(), 287, self.get_y())
        self.ln(2)

    def footer(self):
        self.set_y(-10)
        self.set_font("Arial", "I", 8)
        self.set_text_color(128)
        self.cell(0, 10, f"Página {self.page_no()}", 0, 0, "C")

def crear_pdf_pd_excel(df_data, anio, meses_filtrados):
    pdf = ReportePD(orientation='L', unit='mm', format='A4')
    pdf.add_page()
    meses_letras = ['E', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D']
    num_meses = len(meses_filtrados)

    def generar_grafico_tendencia_pdf(df, col_real, obj_t, obj_c, is_pct):
        df_plot = df[df['Mes'].isin(meses_filtrados)].copy()
        
        # Extracción a listas puras para alinear matemáticamente con FPDF
        y_vals = df_plot[col_real].tolist()
        x_vals = list(range(len(y_vals))) 
        
        fig = go.Figure()
        text_format = [f"{v:.1f}%" if is_pct else f"{v:.0f}" for v in y_vals]
        
        # Color verde FAMMA para las barras
        fig.add_trace(go.Bar(
            x=x_vals, y=y_vals, name="Real (A)",
            marker_color='#28a745', text=text_format, textposition='auto', textfont=dict(size=12)
        ))
        fig.add_trace(go.Scatter(
            x=x_vals, y=[obj_t] * len(x_vals), name="Sup. (T)",
            mode='lines', line=dict(color='red', dash='dash', width=2)
        ))
        fig.add_trace(go.Scatter(
            x=x_vals, y=[obj_c] * len(x_vals), name="Inf. (C)",
            mode='lines', line=dict(color='orange', dash='dot', width=2)
        ))
        
        y_title = "Porcentaje (%)" if is_pct else "Minutos"
        
        fig.update_layout(
            yaxis=dict(title=dict(text=y_title, font=dict(size=9)), tickfont=dict(size=8)),
            xaxis=dict(
                type='linear', autorange=False, range=[-0.5, len(x_vals) - 0.5],
                showticklabels=False, showgrid=False, zeroline=False
            ),
            margin=dict(l=50, r=0, t=25, b=0, pad=0, autoexpand=False), 
            height=175, width=590, 
            bargap=0.15,
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, font=dict(size=9)),
            plot_bgcolor='white'
        )
        fig.update_yaxes(showgrid=True, gridwidth=1, gridcolor='LightGray')
        
        tmp_chart = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        fig.write_image(tmp_chart.name)
        return tmp_chart.name

    def dibujar_bloque_completo(x, y, titulo, obj_t, obj_c, col_real, col_acum, is_lower_better, is_pct=False):
        w_lbl = 10      
        w_tot = 118 
        w_m = (w_tot - w_lbl) / num_meses 
        
        pdf.set_xy(x, y)
        pdf.set_font("Arial", 'B', 8)
        # Colores personalizados para las cajas FAMMA
        pdf.set_text_color(255, 255, 255); pdf.set_fill_color(34, 139, 34); pdf.set_draw_color(0, 0, 0); pdf.set_line_width(0.2)
        pdf.cell(w_tot, 5, "  " + titulo, border=1, align='L', fill=True)

        img_path = generar_grafico_tendencia_pdf(df_data, col_real, obj_t, obj_c, is_pct)
        pdf.image(img_path, x=x, y=y + 5, w=w_tot, h=35)
        os.remove(img_path)

        y_tabla = y + 40 
        
        pdf.set_xy(x, y_tabla)
        pdf.set_fill_color(228, 243, 228); pdf.set_text_color(0,0,0) # Fondo verde claro
        pdf.cell(w_lbl, 5, "", border=0, align='C', fill=False) 
        for i in meses_filtrados: 
            m_letra = meses_letras[i-1]
            pdf.cell(w_m, 5, m_letra, border=1, align='C', fill=True)
            
        pdf.set_xy(x, y_tabla + 5)
        pdf.set_font("Arial", 'B', 8)
        pdf.set_fill_color(255, 255, 255)
        pdf.cell(w_lbl, 5, "T", border=1, align='C', fill=True)
        pdf.set_font("Arial", '', 7)
        t_str = f"{obj_t}%" if is_pct else f"{obj_t}"
        for _ in meses_filtrados: 
            pdf.cell(w_m, 5, t_str, border=1, align='C', fill=True) 
            
        pdf.set_xy(x, y_tabla + 10)
        pdf.set_font("Arial", 'B', 8)
        pdf.set_fill_color(228, 243, 228)
        pdf.cell(w_lbl, 5, "C", border=1, align='C', fill=True)
        pdf.set_font("Arial", '', 7)
        c_str = f"{obj_c}%" if is_pct else f"{obj_c}"
        for _ in meses_filtrados: 
            pdf.cell(w_m, 5, c_str, border=1, align='C', fill=True)
            
        pdf.set_xy(x, y_tabla + 15)
        pdf.set_font("Arial", 'B', 8)
        pdf.set_fill_color(255, 255, 255)
        pdf.cell(w_lbl, 5, "A", border=1, align='C', fill=True)
        pdf.set_font("Arial", 'B', 7)
        
        for i in meses_filtrados:
            val_a = df_data[df_data['Mes'] == i][col_real].values[0]
            if df_data[df_data['Mes'] == i]['Tiempo_Total_Disponible_Min'].values[0] > 0:
                val_str = f"{val_a:.1f}%" if is_pct else f"{val_a:.0f}" 
                if is_lower_better:
                    if val_a <= obj_c: pdf.set_text_color(33, 195, 84)        
                    elif val_a > obj_t: pdf.set_text_color(220, 20, 20)      
                    else: pdf.set_text_color(200, 150, 0)                    
                else:
                    if val_a >= obj_t: pdf.set_text_color(33, 195, 84)        
                    elif val_a < obj_c: pdf.set_text_color(220, 20, 20)      
                    else: pdf.set_text_color(200, 150, 0)                    
            else:
                val_str = "-"
                pdf.set_text_color(150, 150, 150) 
            pdf.cell(w_m, 5, val_str, border=1, align='C', fill=True)
        
        pdf.set_text_color(0,0,0) 

    dibujar_bloque_completo(x=20, y=25, titulo="Down Time General", obj_t=TARGET_DT_T, obj_c=TARGET_DT_C, col_real='DT (%)', col_acum='A_DT (%)', is_lower_better=True, is_pct=True)
    dibujar_bloque_completo(x=150, y=25, titulo="MTTR - Tiempo medio parada (Min)", obj_t=TARGET_MTTR_T, obj_c=TARGET_MTTR_C, col_real='MTTR (Min)', col_acum='A_MTTR (Min)', is_lower_better=True)
    dibujar_bloque_completo(x=20, y=95, titulo="MTBF - Tiempo medio entre fallas (Min)", obj_t=TARGET_MTBF_T, obj_c=TARGET_MTBF_C, col_real='MTBF (Min)', col_acum='A_MTBF (Min)', is_lower_better=False)

    return pdf.output(dest='S').encode('latin-1')

# ==========================================
# SECCIÓN DE DESCARGA
# ==========================================
if not df_anual.empty:
    st.write("📥 **Generar Reporte**")
    
    try:
        pdf_bytes = crear_pdf_pd_excel(df_anual, anio_sel, meses_activos)
        
        area_descarga = "General" if area_sel == "Ambas (General)" else area_sel
        nombres_meses_str = "_".join(meses_sel) if meses_sel else "Varios"
        
        st.download_button(
            label=f"📄 Descargar Reporte PDF ({area_descarga})",
            data=pdf_bytes,
            file_name=f"FAMMA_MTTR_MTBF_{nombres_meses_str}_{anio_sel}.pdf",
            mime="application/pdf"
        )
    except Exception as e:
        st.error(f"Error al generar PDF: {e}")
else:
    st.warning("No hay datos disponibles para el año y área seleccionados.")
