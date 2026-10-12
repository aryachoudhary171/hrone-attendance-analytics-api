import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv()
client=MongoClient(os.getenv('MONGO_URI','mongodb://localhost:27017'),tz_aware=True,serverSelectionTimeoutMS=3000)
db=client[os.getenv('MONGO_DB','attendance_db')]
IST=timezone(timedelta(hours=5,minutes=30)); UTC=timezone.utc
PRESENT=['PRESENT','WFH','ON_DUTY']; STATUSES=PRESENT+['ABSENT','LEAVE']
app=FastAPI(title='Employee Attendance & Analytics API',version='2.0.0')

@app.on_event('startup')
def ensure_indexes():
    db.employees.create_index([('emp_code',1)],unique=True,name='uq_employee_code')
    db.employees.create_index([('department',1),('joined_on',1),('emp_code',1)],name='department_joined_code')
    db.employees.create_index([('joined_on',1),('emp_code',1)],name='employee_joined_code')
    db.attendance_logs.create_index([('emp_code',1),('date',1)],unique=True,name='uq_attendance_employee_date')
    db.attendance_logs.create_index([('date',-1),('emp_code',1)],name='attendance_date_code')
    db.attendance_logs.create_index([('date',1),('status',1),('emp_code',1)],name='attendance_date_status_code')

def now(): return datetime.now(UTC).replace(microsecond=0)
def msdt(v):
    if type(v) is not int or not 100000000000<=v<=4102444800000: raise ValueError('timestamp must be supported epoch milliseconds')
    return datetime.fromtimestamp(v//1000,UTC)
def toms(v):
    if v is None:return None
    if isinstance(v,int):return int(v)
    if v.tzinfo is None:v=v.replace(tzinfo=UTC)
    return int(v.timestamp()*1000)
def outdoc(d):
    d={k:v for k,v in d.items() if k!='_id'}
    d['punch_in']=toms(d.get('punch_in'));d['punch_out']=toms(d.get('punch_out'));d['half_day']=d.get('half_day',False);d['history']=d.get('history') or []
    for h in d['history']:
        h['at']=toms(h.get('at'))
        for k,v in h.get('changes',{}).items():
            if k in ('punch_in','punch_out'):v['from']=toms(v.get('from'));v['to']=toms(v.get('to'))
    return d
def day(s):
    try:return date.fromisoformat(s)
    except (ValueError,TypeError):raise ValueError('date must be YYYY-MM-DD')
def bounds(month):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])',month or ''):raise HTTPException(422,'month must be YYYY-MM')
    y,m=map(int,month.split('-'));return date(y,m,1),date(y,m,calendar.monthrange(y,m)[1])
def rnd(v,n=2):return float(Decimal(str(v)).quantize(Decimal('1').scaleb(-n),rounding=ROUND_HALF_UP))
def halfup_expr(value,places):
    scale=10**places
    return {'$divide':[{'$floor':{'$add':[{'$multiply':[value,scale]},0.5]}},scale]}
def shiftdt(d,s):
    h,m=map(int,s.split(':'));return datetime.combine(d,time(h,m),IST).astimezone(UTC)
def attendance_day(pin,e):
    x=pin.astimezone(IST);d=x.date()
    if e['shift_end']<=e['shift_start'] and x.strftime('%H:%M')<e['shift_end']:d-=timedelta(days=1)
    return d
def calc(pin,pout,e,d):
    if pin is None:return dict(late_minutes=0,work_hours=None,overtime_minutes=0,half_day=False)
    late_s=int((pin-shiftdt(d,e['shift_start'])).total_seconds());late=late_s//60 if late_s>600 else 0
    if pout is None:return dict(late_minutes=late,work_hours=None,overtime_minutes=0,half_day=False)
    wh=rnd(Decimal(int((pout-pin).total_seconds()))/Decimal(3600));ed=d+(timedelta(days=1) if e['shift_end']<=e['shift_start'] else timedelta())
    ot=int((pout-shiftdt(ed,e['shift_end'])).total_seconds())//60
    return dict(late_minutes=late,work_hours=wh,overtime_minutes=ot if ot>=30 else 0,half_day=wh<4.5)

class M(BaseModel): model_config=ConfigDict(extra='ignore')
class EmployeeIn(M):
    emp_code:str=Field(pattern=r'^EMP\d{4,6}$');name:str=Field(min_length=1,max_length=100)
    email:str=Field(max_length=120,pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$');department:str=Field(min_length=1,max_length=50)
    shift_start:str=Field('09:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$');shift_end:str=Field('18:30',pattern=r'^([01]\d|2[0-3]):[0-5]\d$');joined_on:str
    @field_validator('joined_on')
    @classmethod
    def valid_date(cls,v):day(v);return v
    @model_validator(mode='after')
    def shifts(self):
        if self.shift_start==self.shift_end:raise ValueError('shift times must differ')
        return self
class Pin(M):
    emp_code:str;punched_at:Optional[StrictInt]=None;status:Literal['PRESENT','WFH','ON_DUTY']='PRESENT'
    @model_validator(mode='before')
    @classmethod
    def reject_null_timestamp(cls,values):
        if isinstance(values,dict) and 'punched_at' in values and values['punched_at'] is None:raise ValueError('punched_at cannot be null')
        return values
    @field_validator('punched_at')
    @classmethod
    def valid_ts(cls,v):
        if v is not None:msdt(v)
        return v
class Pout(M):
    emp_code:str;punched_at:Optional[StrictInt]=None
    @model_validator(mode='before')
    @classmethod
    def reject_null_timestamp(cls,values):
        if isinstance(values,dict) and 'punched_at' in values and values['punched_at'] is None:raise ValueError('punched_at cannot be null')
        return values
    @field_validator('punched_at')
    @classmethod
    def valid_ts(cls,v):
        if v is not None:msdt(v)
        return v
class Correction(M):
    status:Optional[Literal['PRESENT','WFH','ON_DUTY','ABSENT','LEAVE']]=None;punch_in:Optional[StrictInt]=None;punch_out:Optional[StrictInt]=None
    reason:str=Field(min_length=5,max_length=200);regularized_by:str=Field(min_length=1,max_length=50)
    @model_validator(mode='before')
    @classmethod
    def reject_explicit_nulls(cls,values):
        if isinstance(values,dict):
            for key in ('status','punch_in','punch_out'):
                if key in values and values[key] is None:raise ValueError(f'{key} cannot be null')
        return values
    @field_validator('punch_in','punch_out')
    @classmethod
    def valid_ts(cls,v):
        if v is not None:msdt(v)
        return v
def getemp(code):
    e=db.employees.find_one({'emp_code':code},{'_id':0})
    if not e:raise HTTPException(404,'employee not found')
    return e
def pagecheck(p,s):
    if p<1 or not 1<=s<=100:raise HTTPException(422,'invalid pagination')

@app.get('/health')
def health():
    try:client.admin.command('ping');return {'status':'ok'}
    except PyMongoError:raise HTTPException(503,'MongoDB unavailable')
@app.post('/employees',status_code=201)
def create_employee(b:EmployeeIn):
    d=b.model_dump();d['created_at']=now()
    try:db.employees.insert_one(d)
    except DuplicateKeyError:raise HTTPException(409,'emp_code already exists')
    d.pop('_id',None);d['created_at']=toms(d['created_at']);return d
@app.get('/employees')
def employees(department:Optional[str]=None,page:int=1,page_size:int=20):
    pagecheck(page,page_size);q={'department':department} if department is not None else {};total=db.employees.count_documents(q)
    xs=list(db.employees.find(q,{'_id':0}).sort('emp_code',1).skip((page-1)*page_size).limit(page_size))
    for x in xs:x['created_at']=toms(x['created_at'])
    return {'items':xs,'total':total,'page':page,'page_size':page_size}
@app.post('/attendance/punch-in',status_code=201)
def punchin(b:Pin):
    e=getemp(b.emp_code);ts=msdt(b.punched_at) if b.punched_at is not None else now();d=attendance_day(ts,e)
    doc={'emp_code':b.emp_code,'date':d.isoformat(),'status':b.status,'punch_in':ts,'punch_out':None,'work_hours':None,**calc(ts,None,e,d),'history':[]}
    try:db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:raise HTTPException(409,'already punched in for this date')
    return outdoc(doc)
@app.get('/attendance')
def attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=1,page_size:int=20):
    pagecheck(page,page_size);q={}
    if emp_code is not None:q['emp_code']=emp_code
    if status is not None:
        if status not in STATUSES:raise HTTPException(422,'invalid status')
        q['status']=status
    if date_from or date_to:
        z={}
        try:
            if date_from:day(date_from);z['$gte']=date_from
            if date_to:day(date_to);z['$lte']=date_to
        except ValueError as ex:raise HTTPException(422,str(ex))
        if date_from and date_to and date_from>date_to:raise HTTPException(422,'invalid date range')
        q['date']=z
    total=db.attendance_logs.count_documents(q);cur=db.attendance_logs.find(q).sort([('date',-1),('emp_code',1)]).skip((page-1)*page_size).limit(page_size)
    return {'items':[outdoc(x) for x in cur],'total':total,'page':page,'page_size':page_size}
@app.post('/attendance/punch-out')
def punchout(b:Pout):
    e=getemp(b.emp_code);ts=msdt(b.punched_at) if b.punched_at is not None else now();r=db.attendance_logs.find_one({'emp_code':b.emp_code,'punch_in':{'$lte':ts}},sort=[('punch_in',-1)])
    if not r:raise HTTPException(404,'no punch-in found')
    if r.get('punch_out') is not None:raise HTTPException(409,'already punched out')
    pin=r['punch_in']
    if ts<=pin or ts-pin>timedelta(hours=24):raise HTTPException(422,'invalid punch-out time')
    vals=calc(pin,ts,e,day(r['date']));u=db.attendance_logs.find_one_and_update({'emp_code':b.emp_code,'date':r['date'],'punch_out':None},{'$set':{'punch_out':ts,**vals}},return_document=ReturnDocument.AFTER)
    if not u:raise HTTPException(409,'already punched out')
    return outdoc(u)
def nv(v,k):
    if k in ('punch_in','punch_out') and isinstance(v,datetime):return toms(v)
    if k=='half_day' and v is None:return False
    if k in ('late_minutes','overtime_minutes') and v is None:return 0
    return v
@app.patch('/attendance/{emp_code}/{date}')
def regularize(emp_code:str,date:str,b:Correction):
    e=getemp(emp_code)
    try:d=day(date)
    except ValueError as ex:raise HTTPException(422,str(ex))
    r=db.attendance_logs.find_one({'emp_code':emp_code,'date':date})
    if not r:raise HTTPException(404,'attendance record not found')
    st=b.status or r['status'];pin=msdt(b.punch_in) if b.punch_in is not None else r.get('punch_in');pout=msdt(b.punch_out) if b.punch_out is not None else r.get('punch_out')
    if st in ('ABSENT','LEAVE'):
        if b.punch_in is not None or b.punch_out is not None:raise HTTPException(422,'non-presence status cannot have punch times')
        pin=pout=None
    elif pin is None:raise HTTPException(422,'presence status requires punch_in')
    if pin is not None and attendance_day(pin,e)!=d:raise HTTPException(422,'punch_in must belong to attendance date')
    if pout is not None and (pin is None or pout<=pin or pout-pin>timedelta(hours=24)):raise HTTPException(422,'invalid punch_out')
    new={'status':st,'punch_in':pin,'punch_out':pout,**calc(pin,pout,e,d)}
    changes={k:{'from':r.get(k,False if k=='half_day' else 0 if k in ('late_minutes','overtime_minutes') else None),'to':v}
             for k,v in new.items() if nv(r.get(k),k)!=nv(v,k)}
    if not changes:raise HTTPException(422,'correction changes nothing')
    filt={'emp_code':emp_code,'date':date}
    for k in new:filt[k]=r[k] if k in r else {'$exists':False}
    filt['history']=r['history'] if 'history' in r else {'$exists':False}
    entry={'at':now(),'by':b.regularized_by,'reason':b.reason,'changes':changes}
    u=db.attendance_logs.find_one_and_update(filt,{'$set':new,'$push':{'history':entry}},return_document=ReturnDocument.AFTER)
    if not u:raise HTTPException(409,'attendance record changed concurrently')
    return outdoc(u)

def wd(start,end):
    n={'$dateDiff':{'startDate':start,'endDate':{'$dateAdd':{'startDate':end,'unit':'day','amount':1}},'unit':'day'}}
    return {'$let':{'vars':{'n':n,'dow':{'$mod':[{'$add':[{'$dayOfWeek':start},5]},7]}},'in':{'$add':[{'$multiply':[{'$floor':{'$divide':['$$n',7]}},5]},{'$size':{'$filter':{'input':{'$range':[0,{'$mod':['$$n',7]}]},'as':'i','cond':{'$lt':[{'$mod':[{'$add':['$$dow','$$i']},7]},5]}}}}]}}}
def monthly_pipe(code,month):
    a,b=bounds(month);lo,hi=a.isoformat(),b.isoformat()
    return [{' $match':{'emp_code':code}}] if False else [
      {'$match':{'emp_code':code}},
      {'$lookup':{'from':'attendance_logs','let':{'c':'$emp_code'},'pipeline':[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$c']},{'$gte':['$date',lo]},{'$lte':['$date',hi]}]}}},
       {'$group':{'_id':None,'present':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENT]},{'$in':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},[2,3,4,5,6]]}]},{'$cond':[{'$ifNull':['$half_day',False]},0.5,1]},0]}},'leave':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'late':{'$sum':{'$ifNull':['$late_minutes',0]}},'ot':{'$sum':{'$ifNull':['$overtime_minutes',0]}}}}],'as':'s'}},
      {'$addFields':{'_start':{'$max':[{'$dateFromString':{'dateString':lo}},{'$dateFromString':{'dateString':'$joined_on'}}]},'_end':{'$dateFromString':{'dateString':hi}}}},
      {'$addFields':{'working_days':{'$cond':[{'$gt':['$_start','$_end']},0,wd('$_start','$_end')]},'_s':{'$ifNull':[{'$arrayElemAt':['$s',0]},{}]}}},
      {'$project':{'_id':0,'emp_code':1,'month':{'$literal':month},'working_days':{'$toInt':'$working_days'},'present_days':{'$ifNull':['$_s.present',0]},'leave_days':{'$ifNull':['$_s.leave',0]},'late_count':{'$ifNull':['$_s.late_count',0]},'total_late_minutes':{'$ifNull':['$_s.late',0]},'total_overtime_minutes':{'$ifNull':['$_s.ot',0]},'attendance_pct':{'$cond':[{'$gt':['$working_days',0]},halfup_expr({'$multiply':[{'$divide':[{'$ifNull':['$_s.present',0]},'$working_days']},100]},2),None]}}}]
def dept_pipe(month,dept):
    a,b=bounds(month);lo,hi=a.isoformat(),b.isoformat();match={'joined_on':{'$lte':hi}}
    if dept is not None:match['department']=dept
    sub=[{'$match':{'$expr':{'$and':[{'$eq':['$emp_code','$$c']},{'$gte':['$date',lo]},{'$lte':['$date',hi]}]}}},
     {'$group':{'_id':None,'p':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENT]},{'$in':[{'$dayOfWeek':{'$dateFromString':{'dateString':'$date'}}},[2,3,4,5,6]]}]},{'$cond':[{'$ifNull':['$half_day',False]},0.5,1]},0]}},'hs':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENT]},{'$ne':['$work_hours',None]}]},'$work_hours',0]}},'hc':{'$sum':{'$cond':[{'$and':[{'$in':['$status',PRESENT]},{'$ne':['$work_hours',None]}]},1,0]}},'lc':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}},'lm':{'$sum':{'$ifNull':['$late_minutes',0]}},'leave':{'$sum':{'$cond':[{'$eq':['$status','LEAVE']},1,0]}},'duty':{'$sum':{'$cond':[{'$eq':['$status','ON_DUTY']},1,0]}}}}]
    return [{'$match':match},{'$lookup':{'from':'attendance_logs','let':{'c':'$emp_code'},'pipeline':sub,'as':'s'}},
     {'$group':{'_id':'$department','headcount':{'$sum':1},'present_days':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.p',0]},0]}},'hs':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.hs',0]},0]}},'hc':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.hc',0]},0]}},'late_count':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.lc',0]},0]}},'total_late_minutes':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.lm',0]},0]}},'leave_count':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.leave',0]},0]}},'on_duty_count':{'$sum':{'$ifNull':[{'$arrayElemAt':['$s.duty',0]},0]}}}},
     {'$project':{'_id':0,'department':'$_id','headcount':1,'present_days':1,'avg_work_hours':{'$cond':[{'$gt':['$hc',0]},halfup_expr({'$divide':['$hs','$hc']},2),None]},'late_count':1,'total_late_minutes':1,'leave_count':1,'on_duty_count':1}},{'$sort':{'department':1}}]
def leaderboard_pipe(month,dept,limit):
    a,b=bounds(month);p=[{'$match':{'date':{'$gte':a.isoformat(),'$lte':b.isoformat()}}},{'$group':{'_id':'$emp_code','total_late_minutes':{'$sum':{'$ifNull':['$late_minutes',0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},{'$match':{'total_late_minutes':{'$gt':0}}},{'$lookup':{'from':'employees','localField':'_id','foreignField':'emp_code','as':'e'}},{'$unwind':'$e'}]
    if dept is not None:p.append({'$match':{'e.department':dept}})
    return p+[{'$setWindowFields':{'sortBy':{'total_late_minutes':-1},'output':{'rank':{'$rank':{}}}}},{'$match':{'rank':{'$lte':limit}}},{'$sort':{'total_late_minutes':-1,'_id':1}},{'$project':{'_id':0,'rank':1,'emp_code':'$_id','name':'$e.name','department':'$e.department','total_late_minutes':1,'late_count':1}}]
def trend_pipe(dept,start,end):
    a=datetime.combine(start,time.min,UTC);b=datetime.combine(end+timedelta(days=1),time.min,UTC)
    return [{'$match':{'date':{'$gte':start.isoformat(),'$lte':end.isoformat()}}},{'$lookup':{'from':'employees','localField':'emp_code','foreignField':'emp_code','as':'e'}},{'$unwind':'$e'},{'$match':{'e.department':dept}},
     {'$group':{'_id':'$date','present_count':{'$sum':{'$cond':[{'$in':['$status',PRESENT]},{'$cond':[{'$ifNull':['$half_day',False]},0.5,1]},0]}},'late_count':{'$sum':{'$cond':[{'$gt':[{'$ifNull':['$late_minutes',0]},0]},1,0]}}}},
     {'$unionWith':{'coll':'employees','pipeline':[{'$match':{'department':dept}},{'$limit':1},{'$project':{'_id':{'$literal':start.isoformat()},'present_count':{'$literal':0},'late_count':{'$literal':0}}}]}},
     {'$group':{'_id':'$_id','present_count':{'$sum':'$present_count'},'late_count':{'$sum':'$late_count'}}},
     {'$project':{'_id':0,'date':{'$dateFromString':{'dateString':'$_id'}},'present_count':1,'late_count':1}},{'$densify':{'field':'date','range':{'step':1,'unit':'day','bounds':[a,b]} }},{'$fill':{'output':{'present_count':{'value':0},'late_count':{'value':0}}}},
     {'$lookup':{'from':'employees','let':{'ds':{'$dateToString':{'date':'$date','format':'%Y-%m-%d','timezone':'UTC'}}},'pipeline':[{'$match':{'department':dept}},{'$match':{'$expr':{'$lte':['$joined_on','$$ds']}}},{'$count':'n'}],'as':'hc'}},
     {'$addFields':{'ds':{'$dateToString':{'date':'$date','format':'%Y-%m-%d','timezone':'UTC'}},'headcount':{'$ifNull':[{'$arrayElemAt':['$hc.n',0]},0]},'is_working_day':{'$and':[{'$gte':[{'$dayOfWeek':'$date'},2]},{'$lte':[{'$dayOfWeek':'$date'},6]}]}}},
     {'$addFields':{'attendance_rate':{'$cond':[{'$and':['$is_working_day',{'$gt':['$headcount',0]}]},halfup_expr({'$divide':['$present_count','$headcount']},4),None]}}},
     {'$setWindowFields':{'sortBy':{'date':1},'output':{'moving_avg_7d':{'$avg':'$attendance_rate','window':{'documents':[-6,0]}}}}},
     {'$project':{'_id':0,'date':'$ds','is_working_day':1,'headcount':1,'present_count':1,'late_count':1,'attendance_rate':1,'moving_avg_7d':halfup_expr('$moving_avg_7d',4)}},{'$sort':{'date':1}}]

@app.get('/analytics/employees/{emp_code}/monthly')
def employee_monthly(emp_code:str,month:str):
    getemp(emp_code);bounds(month);return list(db.employees.aggregate(monthly_pipe(emp_code,month)))[0]
@app.get('/analytics/departments/summary')
def department_summary(month:str,department:Optional[str]=None):
    bounds(month);hint='department_joined_code' if department is not None else 'employee_joined_code'
    return {'month':month,'items':list(db.employees.aggregate(dept_pipe(month,department),hint=hint))}
@app.get('/analytics/leaderboard/late')
def leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    bounds(month);return {'month':month,'items':list(db.attendance_logs.aggregate(leaderboard_pipe(month,department,limit)))}
@app.get('/analytics/departments/{department}/trend')
def trend(department:str,from_:str=Query(alias='from'),to:str=Query()):
    try:a,b=day(from_),day(to)
    except ValueError as e:raise HTTPException(422,str(e))
    if b<a or (b-a).days+1>92:raise HTTPException(422,'invalid trend range')
    if not db.employees.count_documents({'department':department}):raise HTTPException(404,'department not found')
    return {'department':department,'items':list(db.attendance_logs.aggregate(trend_pipe(department,a,b)))}

def explain(coll,pipeline=None,q=None,sort=None,skip=0,limit=None,hint=None):
    if pipeline is not None:
        cmd={'aggregate':coll.name,'pipeline':pipeline,'cursor':{}}
        if hint is not None:cmd['hint']=hint
    else:
        cmd={'find':coll.name,'filter':q or {},'sort':dict(sort or [])}
        if skip:cmd['skip']=skip
        if limit is not None:cmd['limit']=limit
    return db.command('explain',cmd,verbosity='executionStats')
@app.get('/admin/explain/{endpoint}')
def explain_ep(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=1,page_size:int=20,from_:Optional[str]=Query(None,alias='from'),to:Optional[str]=None):
    pagecheck(page,page_size)
    if endpoint=='attendance_list':
        q={}
        if emp_code is not None:q['emp_code']=emp_code
        if status is not None:
            if status not in STATUSES:raise HTTPException(422,'invalid status')
            q['status']=status
        if date_from or date_to:
            q['date']={}
            try:
                if date_from:day(date_from);q['date']['$gte']=date_from
                if date_to:day(date_to);q['date']['$lte']=date_to
            except ValueError as e:raise HTTPException(422,str(e))
            if date_from and date_to and date_from>date_to:raise HTTPException(422,'invalid date range')
        p=explain(db.attendance_logs,q=q,sort=[('date',-1),('emp_code',1)],skip=(page-1)*page_size,limit=page_size);c='attendance_logs'
    elif endpoint=='employee_monthly':
        if not emp_code or not month:raise HTTPException(422,'emp_code and month required')
        p=explain(db.employees,monthly_pipe(emp_code,month));c='employees'
    elif endpoint=='department_summary':
        if not month:raise HTTPException(422,'month required')
        hint='department_joined_code' if department is not None else 'employee_joined_code'
        p=explain(db.employees,dept_pipe(month,department),hint=hint);c='employees'
    elif endpoint=='late_leaderboard':
        if not month:raise HTTPException(422,'month required')
        p=explain(db.attendance_logs,leaderboard_pipe(month,department,limit));c='attendance_logs'
    elif endpoint=='department_trend':
        if not department or not from_ or not to:raise HTTPException(422,'department, from and to required')
        try:a,b=day(from_),day(to)
        except ValueError as e:raise HTTPException(422,str(e))
        if b<a or (b-a).days+1>92:raise HTTPException(422,'invalid date range')
        p=explain(db.attendance_logs,trend_pipe(department,a,b));c='attendance_logs'
    else:raise HTTPException(422,'unknown endpoint')
    return {'endpoint':endpoint,'collection':c,'explain':json_util.loads(json_util.dumps(p))}
